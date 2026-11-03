"""
Публикация кадров в RTSP-сервер (MediaMTX) через ffmpeg.

* Кодирование: h264_nvenc (GPU) или libx264 (CPU); config['rtsp_server']['encoder']: auto | nvenc | x264.
  В режиме auto NVENC проверяется реальным тестовым кодированием теми же параметрами, что и в работе.
* put_frame() никогда не блокирует цикл распознавания: очередь на 2 кадра, старый кадр выбрасывается.
* За жизнь ffmpeg отвечает один поток - гонок при переподключении нет.
"""
import logging
import queue
import subprocess
import threading
import time

import cv2
import numpy as np

log = logging.getLogger("face_access.stream")


class RTSPStreamer:
    def __init__(self, rtsp_config):
        self.rtsp_url = rtsp_config.get("url")
        if not self.rtsp_url:
            raise ValueError("rtsp_server.url must be specified in config")
        self.frame_size = (int(rtsp_config.get("width", 1280)), int(rtsp_config.get("height", 720)))
        self.fps = int(rtsp_config.get("fps", 25))
        self.bitrate = str(rtsp_config.get("bitrate", "3M"))
        self.encoder_setting = str(rtsp_config.get("encoder", "auto")).lower()
        self.encoder = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.running = False
        self.ffmpeg_process = None
        self._thread = None
        self._started = None

    # ------------------------------------------------------------------ запуск / остановка
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._stream_loop, daemon=True, name="rtsp-streamer")
        self._thread.start()
        log.info("Starting RTSP stream to %s", self.rtsp_url)

    def stop(self):
        self.running = False
        proc = self.ffmpeg_process
        if proc:
            self._kill(proc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        log.info("RTSP stream stopped")

    # ------------------------------------------------------------------ выбор кодека
    def _gop(self):
        return str(max(1, self.fps))  # ключевой кадр раз в секунду: зритель не ждёт 10 с

    def _nvenc_args(self):
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-zerolatency", "1",
                "-rc", "cbr", "-b:v", self.bitrate, "-profile:v", "main",
                "-bf", "0", "-g", self._gop(), "-forced-idr", "1"]

    def _x264_args(self):
        return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "main", "-g", self._gop()]

    def _nvenc_works(self):
        """Реальная проверка: кодируем несколько кадров теми же параметрами, что и в работе."""
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", f"color=black:s=256x256:r={self.fps}", "-frames:v", "5",
                 *self._nvenc_args(), "-pix_fmt", "yuv420p", "-f", "null", "-"],
                capture_output=True, timeout=25)
            if r.returncode != 0:
                log.warning("NVENC test failed: %s", r.stderr.decode(errors="ignore").strip()[-300:])
            return r.returncode == 0
        except Exception as e:
            log.warning("NVENC test error: %s", e)
            return False

    def _choose_encoder(self):
        if self.encoder_setting == "x264":
            return "libx264"
        if self._nvenc_works():
            return "h264_nvenc"
        if self.encoder_setting == "nvenc":
            log.warning("encoder=nvenc requested, but NVENC is unavailable - falling back to libx264")
        return "libx264"

    def _build_cmd(self, size):
        enc = self._nvenc_args() if self.encoder == "h264_nvenc" else self._x264_args()
        return [
            "ffmpeg", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{size[0]}x{size[1]}",
            "-framerate", str(self.fps),
            "-use_wallclock_as_timestamps", "1",   # время по часам, а не по номеру кадра
            "-i", "-",
            "-r", str(self.fps),                   # постоянная частота на выходе
            *enc,
            "-pix_fmt", "yuv420p",
            "-f", "rtsp", "-rtsp_transport", "tcp", "-muxdelay", "0.1",
            self.rtsp_url,
        ]

    # ------------------------------------------------------------------ основной цикл
    def _stream_loop(self):
        self.encoder = self._choose_encoder()
        log.info("RTSP encoder: %s", self.encoder)
        delay = 1.0
        while self.running:
            self._started = None
            try:
                self._session()
            except Exception as e:
                log.error("Stream error: %s", e)
            if not self.running:
                break
            # сервер недоступен -> пауза растёт до 10 с (лог не засоряется); поработал дольше 10 с -> снова 1 с
            ran = time.monotonic() - self._started if self._started else 0.0
            delay = 1.0 if ran > 10 else min(delay * 2, 10.0) if self._started else 1.0
            time.sleep(delay)

    def _get_frame(self, timeout=0.5):
        try:
            return self.frame_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _session(self):
        frame = None
        while self.running and frame is None:
            frame = self._get_frame()
        if not self.running:
            return

        size = tuple(self.frame_size)
        proc = subprocess.Popen(self._build_cmd(size), stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=10 ** 6)
        self.ffmpeg_process = proc
        self._started = time.monotonic()
        threading.Thread(target=self._monitor_ffmpeg_errors, args=(proc,), daemon=True).start()
        try:
            while self.running and proc.poll() is None:
                if frame is None:
                    frame = self._get_frame()
                    if frame is None:
                        continue
                proc.stdin.write(self._prepare(frame, size).tobytes())
                proc.stdin.flush()
                frame = None
        except (BrokenPipeError, OSError) as e:
            log.error("Pipe error (%s), reconnecting...", e)
        finally:
            self._kill(proc)

    @staticmethod
    def _prepare(frame, size):
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8)
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        if frame.shape[1] != size[0] or frame.shape[0] != size[1]:
            frame = cv2.resize(frame, size)
        return np.ascontiguousarray(frame)

    @staticmethod
    def _monitor_ffmpeg_errors(proc):
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            log.warning("FFmpeg: %s", line.decode(errors="ignore").strip())

    @staticmethod
    def _kill(proc):
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------ API
    def put_frame(self, frame):
        """Неблокирующая отправка: при переполнении очереди старый кадр отбрасывается."""
        if not self.running:
            return
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frame_queue.put_nowait(frame)
            except queue.Full:
                pass
