"""Камера: чтение RTSP в отдельном потоке (всегда свежий кадр + автопереподключение), зоны турникета, разметка кадра."""
import logging
import os
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .utils import DoorState

log = logging.getLogger("face_access.camera")

COLOR_EXIT = (0, 255, 255)   # BGR
COLOR_ENTRANCE = (255, 0, 0)
COLOR_KNOWN = (0, 180, 0)
COLOR_UNKNOWN = (0, 0, 255)


class Camera:
    def __init__(self, config, stop_event):
        cam = config.get("camera", {})
        self.stop_event = stop_event
        cid = cam.get("id")
        self.url = int(cid) if isinstance(cid, str) and cid.strip().isdigit() else cid
        self.transport = cam.get("rtsp_transport")  # tcp | udp | None (по умолчанию ffmpeg)
        self.reduce = max(1, int(cam.get("reduce_frame", 2)))
        self.frame_mode = cam.get("frame_mode", "center")
        tur = config["turnstiles"]
        self._rel_areas = (tur["area_1"], tur["area_2"])
        self._labels = (cam.get("EXIT", "EXIT"), cam.get("ENTRANCE", "ENTRANCE"))
        self._font_path = self._check_font(cam.get("FONT_PATH_RUS"))
        self._fonts = {}

        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0
        self._seen = 0
        self._thread = None
        self.connected = False
        self.fps = 0.0
        self._areas = None
        self._size = None

    # ------------------------------------------------------------------ чтение камеры
    def start(self):
        self._thread = threading.Thread(target=self._reader, daemon=True, name="camera-reader")
        self._thread.start()

    def release(self):
        with self._cond:
            self._cond.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)

    def _open(self):
        if isinstance(self.url, str) and self.url.startswith(("rtsp", "http")):
            if self.transport:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{self.transport}"
            try:  # таймауты: зависший поток не блокирует чтение навсегда
                cap = cv2.VideoCapture(self.url, cv2.CAP_ANY, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                                                               cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
            except (TypeError, cv2.error):
                cap = cv2.VideoCapture(self.url)
        else:
            cap = cv2.VideoCapture(self.url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _set_connected(self, state, reason=""):
        if state != self.connected:
            self.connected = state
            if state:
                log.info("Camera connected")
            else:
                log.warning("Camera not available%s", f": {reason}" if reason else "")

    def _reader(self):
        delay = 1.0
        while not self.stop_event.is_set():
            cap = self._open()
            if not cap.isOpened():
                cap.release()
                self._set_connected(False, "cannot open stream")
                self.stop_event.wait(delay)
                delay = min(delay * 2, 5.0)
                continue
            delay = 1.0
            fails, count, t0 = 0, 0, time.monotonic()
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails >= 3:
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                self._set_connected(True)
                with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._cond.notify_all()
                count += 1
                if count == 30:
                    now = time.monotonic()
                    self.fps = count / (now - t0)
                    count, t0 = 0, now
            cap.release()
            self._set_connected(False, "stream lost, reconnecting")
            self.stop_event.wait(1.0)

    def video_capture(self, timeout=1.0):
        """(True, кадр) - новый кадр; (False, None) - кадра нет (камера недоступна / остановка)."""
        with self._cond:
            ok = self._cond.wait_for(lambda: self._seq != self._seen or self.stop_event.is_set(), timeout)
            if not ok or self.stop_event.is_set() or self._frame is None:
                return False, None
            self._seen = self._seq
            frame = self._frame
        self._ensure_areas(frame)
        return True, frame


    # ------------------------------------------------------------------ геометрия
    def _ensure_areas(self, frame):
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self._size = (w, h)
            self._areas = [[int(a[0] * w), int(a[1] * h), int((a[0] + a[2]) * w), int((a[1] + a[3]) * h)]
                           for a in self._rel_areas]

    def shrink(self, frame):
        if self.reduce == 1:
            return frame
        return cv2.resize(frame, None, fx=1 / self.reduce, fy=1 / self.reduce)

    def to_full(self, locations):
        """Координаты лиц из уменьшенного кадра -> в координаты полного кадра."""
        loc = np.asarray(locations, dtype=np.float32).reshape(-1, 4)
        return loc * self.reduce

    def _in_area(self, loc, area):
        top, left, bottom, right = loc
        if self.frame_mode == "center":
            return area[0] < (left + right) / 2 < area[2] and area[1] < (top + bottom) / 2 < area[3]
        return area[0] < left and right < area[2] and area[1] < top and bottom < area[3]

    def check_areas(self, face_locations, user_ids):
        """Какое распознанное лицо стоит в зоне турникета -> (индекс лица, DoorState). Берётся самое крупное."""
        best, best_size, state = None, -1.0, DoorState.CLOSED
        if self._areas is None:
            return None, state
        for i, (loc, uid) in enumerate(zip(face_locations, user_ids)):
            if uid == 0:
                continue
            for st, area in ((DoorState.EXIT, self._areas[0]), (DoorState.ENTRANCE, self._areas[1])):
                if self._in_area(loc, area):
                    size = (loc[2] - loc[0]) * (loc[3] - loc[1])
                    if size > best_size:
                        best, best_size, state = i, size, st
                    break
        return best, state

    # ------------------------------------------------------------------ рисование
    @staticmethod
    def _check_font(path):
        if not path or str(path) == "None":
            return None
        if os.path.exists(path):
            return path
        log.warning("Font %s not found (apt install fonts-dejavu-core): Cyrillic names will be shown as IDs", path)
        return None

    def _font(self, frame_h):
        size = max(14, frame_h // 36)
        if size not in self._fonts:
            self._fonts[size] = ImageFont.truetype(self._font_path, size)
        return self._fonts[size]

    def _text(self, frame, text, x, y, fg=(255, 255, 255), bg=None):
        """Подпись на кадре; кириллица через PIL, если задан шрифт. Рисуется только нужный прямоугольник."""
        H, W = frame.shape[:2]
        if self._font_path:
            font = self._font(H)
            l, t, r, b = font.getbbox(text)
            w, h = r - l + 8, b - t + 8
            x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
            if x1 <= x0 or y1 <= y0:
                return
            roi = frame[y0:y1, x0:x1]
            if bg is not None:
                roi[:] = bg
            img = Image.fromarray(np.ascontiguousarray(roi[:, :, ::-1]))
            ImageDraw.Draw(img).text((x - x0 + 4 - l, y - y0 + 4 - t), text, font=font, fill=fg[::-1])
            roi[:] = np.asarray(img)[:, :, ::-1]
        else:
            scale = max(0.5, H / 1080 * 0.9)
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
            if bg is not None:
                cv2.rectangle(frame, (x, y), (x + tw + 8, y + th + 10), bg, cv2.FILLED)
            cv2.putText(frame, text, (x + 4, y + th + 3), cv2.FONT_HERSHEY_SIMPLEX, scale, fg, 1, cv2.LINE_AA)

    def _label(self, uid, users):
        name = str(users.get(uid, users.get(0, "?")))
        if self._font_path or name.isascii():
            return name
        return f"ID {uid}" if uid else "?"

    def annotate(self, frame, face_locations, user_ids, users):
        """Рисует зоны турникета и подписи лиц прямо на кадре (frame изменяется)."""
        if self._areas:
            for area, color, text_color, label in zip(self._areas, (COLOR_EXIT, COLOR_ENTRANCE),
                                                      ((0, 0, 0), (255, 255, 255)), self._labels):
                cv2.rectangle(frame, (area[0], area[1]), (area[2], area[3]), color, 2)
                self._text(frame, str(label), area[0] + 2, area[1] + 2, text_color, color)
        H = frame.shape[0]
        bar = max(24, H // 26)
        for (top, left, bottom, right), uid in zip(face_locations, user_ids):
            top, left, bottom, right = int(top), int(left), int(bottom), int(right)
            color = COLOR_UNKNOWN if uid == 0 else COLOR_KNOWN
            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
            cv2.rectangle(frame, (left, bottom - bar), (right, bottom), color, cv2.FILLED)
            self._text(frame, self._label(uid, users), left + 2, bottom - bar + 1, (255, 255, 255), color)
        return frame

    def no_signal_frame(self, size):
        w, h = size
        frame = np.zeros((h, w, 3), np.uint8)
        text = "Нет сигнала с камеры" if self._font_path else "NO CAMERA SIGNAL"
        self._text(frame, text, w // 2 - 200, h // 2 - 20, (255, 255, 255))
        return frame
