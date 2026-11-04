"""Сервис распознавания: камера -> движок -> турникет, плюс стрим в MediaMTX. Им управляет веб-интерфейс (main.py)."""
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import urlparse

import cv2
import numpy as np

from .camera import Camera
from .config import load_config, parse_config, write_config_atomic
from .engines import get_engine
from .engines.base import IMAGE_EXT
from .streamer import RTSPStreamer
from .utils import (Connection, DoorState, PercoError, cleanup_old_files, load_all_users, load_divisions,
                    load_users, setup_logger)

log = logging.getLogger("face_access.service")


class AccessService:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = {}
        self.users = {}             # проход по лицу включён: их распознаёт камера
        self.all_users = {}         # все сотрудники: для веб-страницы
        self.engine = None
        self.camera = None
        self.connection = None
        self.streamer = None
        self.thread = None
        self.stop_event = threading.Event()
        self.state = "stopped"      # stopped | starting | running | error
        self.error = None
        self.last_event = None
        self._events = deque()      # журнал проходов (старые -> новые)
        self.events_version = 0     # растёт при каждом новом проходе: страница по нему понимает, что пора обновить таблицу
        self._events_lock = threading.Lock()
        self._events_path = ""
        self._events_keep = 500
        self.divisions = []         # подразделения Perco (для фильтра на странице)
        self.user_division = {}     # {id сотрудника: id подразделения}
        self.divisions_version = 0
        self.divisions_error = None
        self._passes = {}           # проходы из журнала Perco за сегодня: id события -> запись
        self._passes_ready = False  # True после первой успешной загрузки журнала Perco
        self._passes_error = None
        self._passes_loading = False
        self.loop_fps = 0.0
        self._lock = threading.RLock()

    # ================================================================== жизненный цикл
    def start_async(self):
        """Запуск в фоне: веб-интерфейс доступен сразу, даже пока грузится модель или если запуск не удался."""
        threading.Thread(target=self._safe_start, daemon=True, name="service-start").start()

    def _safe_start(self):
        try:
            self.start()
        except Exception:
            pass  # причина уже в логе и в status()['error']

    def start(self):
        with self._lock:
            self.state, self.error = "starting", None
            try:
                self.config = load_config(self.config_path)
            except Exception as e:
                self.state, self.error = "error", f"config: {e}"
                logging.getLogger("face_access").error("Cannot load config: %s", e)
                raise
            self._build()

    def stop(self):
        with self._lock:
            self._teardown()
            self.state = "stopped"

    def _build(self):
        cfg = self.config
        try:
            setup_logger(cfg)
            log.info("=" * 70)       # каждый запуск (и перезапуск после смены конфига) отделяется в журнале
            log.info("Starting: mode=%s source=%s test_mode=%s", cfg["mode"], cfg.get("source", "db"),
                     cfg.get("test_mode", False))
            self._load_events(cfg)
            self.stop_event = threading.Event()
            self.connection = Connection(cfg)
            self.users = load_users(cfg, self.connection)
            self.all_users = load_all_users(cfg, self.users, self.connection)
            self.engine = get_engine(cfg, self.users)
            self.camera = Camera(cfg, self.stop_event)
            rtsp = cfg.get("rtsp_server") or {}
            self.streamer = RTSPStreamer(rtsp) if rtsp.get("enabled") else None
            self.camera.start()
            if self.streamer:
                self.streamer.start()
            self.thread = threading.Thread(
                target=self._loop, name="capture-loop", daemon=True,
                args=(self.stop_event, self.engine, self.camera, self.connection, self.streamer, cfg))
            self.thread.start()
            self.state, self.error = "running", None
            threading.Thread(target=self._refresh_divisions, daemon=True, name="divisions").start()
            self._start_passes(cfg)
            log.info("Service started: %d users recognized by camera (%d in total), %d embeddings",
                     len(self.users) - 1, len(self.all_users), self.engine.embeddings_count)
        except Exception as e:
            log.exception("Service failed to start")
            self.state, self.error = "error", str(e)
            self._teardown()
            raise

    def _teardown(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=8)
        for obj in (self.camera, self.streamer):
            if obj is not None:
                try:
                    obj.release() if isinstance(obj, Camera) else obj.stop()
                except Exception:
                    log.exception("Error while stopping %s", type(obj).__name__)
        self.thread = self.camera = self.streamer = None

    # ================================================================== действия из веб-интерфейса
    def apply_config(self, raw):
        """Проверяет и применяет новый конфиг. При ошибке возвращает прежний и бросает исключение."""
        new_cfg = parse_config(raw)  # ValueError, если конфиг некорректен - ничего не трогаем
        with self._lock:
            old_cfg = self.config
            self._teardown()
            self.config = new_cfg
            try:
                self._build()
            except Exception:
                log.error("New config failed, restoring the previous one")
                self.config = old_cfg
                try:
                    self._build()
                except Exception:
                    log.exception("Previous config also failed")
                raise
            write_config_atomic(self.config_path, raw)

    def sync(self):
        """Перечитать пользователей (БД/Excel), досчитать эмбеддинги для новых фото - без перезапуска камеры."""
        with self._lock:
            if self.engine is None or self.state != "running":
                raise RuntimeError("Сервис не запущен")
            users = load_users(self.config, self.connection)
            all_users = load_all_users(self.config, users, self.connection)
            stats = self.engine.sync(users)
            self.users, self.all_users = users, all_users
            threading.Thread(target=self._refresh_divisions, daemon=True, name="divisions").start()
            return {**stats, "users": len(users) - 1, "users_total": len(all_users),
                    "embeddings": self.engine.embeddings_count}

    def list_users(self):
        with self._lock:
            engine = self.engine
            folder = self.config.get("images_folder", "images")
            stems = set()
            if os.path.isdir(folder):
                stems = {os.path.splitext(f)[0] for f in os.listdir(folder)
                         if os.path.splitext(f)[1].lower() in IMAGE_EXT}
            rows = [{"id": uid, "name": name, "active": uid in self.users, "photo": str(uid) in stems,
                     "embedding": bool(engine and engine.has_embedding(uid)),
                     "division": self.user_division.get(uid)}
                    for uid, name in self.all_users.items() if uid != 0]
        return sorted(rows, key=lambda r: str(r["name"]).lower())

    def add_photo(self, user_id, data):
        """Фото сотрудника из веб-интерфейса (снимок веб-камеры или файл)."""
        with self._lock:
            if self.engine is None or self.state != "running":
                raise RuntimeError("Сервис не запущен")
            if user_id == 0 or user_id not in self.all_users:
                raise KeyError(user_id)
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("Файл не является изображением")
            self.engine.enroll(user_id, img, self.users, name=self.all_users[user_id])
            # active=False: фото сохранено, но камера пропустит человека только после включения прохода по лицу
            return {"user_id": user_id, "name": self.all_users[user_id], "active": user_id in self.users}

    def _reload_users(self):
        """Перечитать списки сотрудников (после изменения отметки в Perco) и обновить базу распознавания."""
        users = load_users(self.config, self.connection)
        all_users = load_all_users(self.config, users, self.connection)
        self.engine.load_embeddings(users, quiet=True)
        self.users, self.all_users = users, all_users

    def _check_ready(self, user_id):
        if self.engine is None or self.state != "running":
            raise RuntimeError("Сервис не запущен")
        if user_id == 0 or user_id not in self.all_users:
            raise KeyError(user_id)

    def enable_pass(self, user_id, data=None):
        """«Включить проход по лицу»: эмбеддинг по фото (имя файла = id) + отметка в Perco + перезагрузка базы.
        Без data используется уже сохранённый эмбеддинг. perco: set | already | skipped (Excel) | error."""
        with self._lock:
            self._check_ready(user_id)
            name = self.all_users[user_id]
            enrolled = False
            if data:
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    raise ValueError("Файл не является изображением")
                self.engine.enroll(user_id, img, self.users, name=name)
                enrolled = True
            elif not self.engine.has_embedding(user_id):
                raise ValueError("Нет фото сотрудника: снимите фото веб-камерой или загрузите файл")

            perco, perco_error = "already", None
            if self.config.get("source", "db") != "db":
                perco = "skipped"
            elif user_id not in self.users:
                try:
                    self.connection.enable_face_pass(user_id)
                    perco = "set"
                    self._reload_users()
                except PercoError as e:
                    perco, perco_error = "error", str(e)
                except Exception as e:  # сеть, неожиданный ответ API
                    log.exception("Cannot enable face pass in Perco for id %s", user_id)
                    perco, perco_error = "error", str(e)
            log.info("Enable face pass id %s (%s): enrolled=%s perco=%s active=%s",
                     user_id, name, enrolled, perco, user_id in self.users)
            return {"user_id": user_id, "name": name, "enrolled": enrolled, "perco": perco,
                    "perco_error": perco_error, "active": user_id in self.users}

    def disable_pass(self, user_id):
        """«Убрать проход по лицу»: фото и эмбеддинг - в архив, отметка в Perco снимается.
        perco: cleared | none (отметки не было) | skipped (Excel) | error."""
        with self._lock:
            self._check_ready(user_id)
            name = self.all_users[user_id]
            was_active = user_id in self.users
            removed = self.engine.remove(user_id, self.users)  # из распознавания человек пропадает сразу
            if not removed and not was_active:
                raise ValueError("У сотрудника нет ни фото, ни включённого прохода - убирать нечего")

            perco, perco_error = "none", None
            if self.config.get("source", "db") != "db":
                perco = "skipped"
            elif was_active:
                try:
                    self.connection.disable_face_pass(user_id)
                    perco = "cleared"
                    self._reload_users()
                except PercoError as e:
                    perco, perco_error = "error", str(e)
                except Exception as e:
                    log.exception("Cannot disable face pass in Perco for id %s", user_id)
                    perco, perco_error = "error", str(e)
            log.info("Disable face pass id %s (%s): removed=%s perco=%s", user_id, name, removed, perco)
            return {"user_id": user_id, "name": name, "removed": removed, "perco": perco,
                    "perco_error": perco_error, "active": user_id in self.users}

    # ================================================================== журнал проходов
    def _load_events(self, cfg):
        """Читает последние проходы из events_file (если задан), чтобы история переживала перезапуск."""
        self._events_path = str(cfg.get("events_file") or "").strip()
        try:
            self._events_keep = max(1, int(cfg.get("events_keep", 500)))
        except (TypeError, ValueError):
            self._events_keep = 500
        with self._events_lock:
            self._events = deque(maxlen=None)
            if self._events_path and os.path.exists(self._events_path):
                try:
                    with open(self._events_path, encoding="utf-8") as f:
                        for line in f:
                            try:
                                ev = json.loads(line)
                                if isinstance(ev, dict) and "ts" in ev:
                                    self._events.append(ev)
                            except ValueError:
                                continue
                    while len(self._events) > self._events_keep:
                        self._events.popleft()
                except OSError:
                    log.exception("Cannot read events file %s", self._events_path)
            self.events_version += 1

    def _add_event(self, ev):
        with self._events_lock:
            self._events.append(ev)
            trimmed = False
            while len(self._events) > self._events_keep:
                self._events.popleft()
                trimmed = True
            self.events_version += 1
            if not self._events_path:
                return
            try:
                if trimmed and self.events_version % 50 == 0:   # изредка укорачиваем файл до последних N проходов
                    tmp = self._events_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        for e in self._events:
                            f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    os.replace(tmp, self._events_path)
                else:
                    with open(self._events_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except OSError:
                log.exception("Cannot write events file %s", self._events_path)

    def _start_passes(self, cfg):
        with self._events_lock:
            self._passes, self._passes_ready, self._passes_error, self._passes_loading = {}, False, None, False
        pc = cfg.get("perco_events") or {}
        if cfg.get("source", "db") != "db" or pc.get("enabled") is False:
            return
        conn = self.connection
        if pc.get("login") and pc.get("password"):
            # у основной учётки (connection.login) может не быть права на журнал событий (ответ 403) -
            # тогда журнал читается отдельной учёткой Perco, только на чтение
            conn = Connection({**cfg, "connection": {**(cfg.get("connection") or {}),
                                                     "login": pc["login"], "password": pc["password"]}})
            log.info("Perco events: using separate account %r", pc["login"])
        threading.Thread(target=self._passes_loop, args=(self.stop_event, conn, pc), daemon=True,
                         name="perco-passes").start()

    def _passes_loop(self, stop, conn, pc):
        """Проходы ВСЕХ сотрудников (карты и лицо) из журнала Perco за сегодня. Первый раз читаем сутки целиком,
        дальше - только свежий хвост и добавляем новые события."""
        try:
            poll = max(2.0, float(pc.get("poll_sec", 5)))
        except (TypeError, ValueError):
            poll = 5.0
        ids = pc.get("pass_event_ids") or [17]            # 17 - «Проход по идентификатору»
        outside = pc.get("outside_zone_id", 1)            # зона «Неконтролируемая территория»
        rd = pc.get("reader_directions") or {1: "entrance", 2: "exit"}   # считыватель/кнопка № -> направление (для ручных открытий)
        reader_dirs = {int(k): ("exit" if str(v).lower().startswith(("exit", "вых")) else "entrance") for k, v in rd.items()}
        day, last_ts, last_err = None, 0.0, None
        while not stop.is_set():
            try:
                now = datetime.now()
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if day != midnight.date():                # первая загрузка или наступили новые сутки
                    day, last_ts, begin = midnight.date(), 0.0, midnight
                    with self._events_lock:
                        self._passes = {}
                else:
                    begin = max(midnight, datetime.fromtimestamp(last_ts - 180))
                first = not self._passes_ready
                if first:
                    log.info("Perco events: loading today's journal...")
                with self._events_lock:
                    self._passes_loading = first
                rows = conn.read_pass_events(begin, midnight + timedelta(days=1) - timedelta(minutes=1), ids, outside,
                                             on_page=self._merge_passes, reader_directions=reader_dirs,
                                             remote_event_id=int(pc.get("remote_event_id", 112)))
                self._merge_passes(rows)          # на случай, если API не вызвал on_page
                with self._events_lock:
                    last_ts = max([last_ts] + [e["ts"] for e in self._passes.values()])
                    self._passes_ready, self._passes_error, self._passes_loading = True, None, False
                    self.events_version += 1
                if first:
                    log.info("Perco events: %d passes loaded", len(self._passes))
                elif last_err:
                    log.info("Perco events: connection restored")
                last_err = None
            except Exception as e:
                msg = str(e)
                with self._events_lock:
                    self._passes_error, self._passes_loading = msg, False
                if msg != last_err:                       # одинаковую ошибку в лог не повторяем каждые 5 секунд
                    log.warning("Cannot read Perco events: %s", msg)
                    if "403" in msg:
                        log.warning("Perco events: у учётной записи нет права на журнал событий. Дайте это право учётке "
                                    "connection.login в Perco или задайте perco_events.login / perco_events.password")
                last_err = msg
                stop.wait(max(poll, 30.0))                # при ошибке - редкие повторы, не нагружаем Perco
                continue
            stop.wait(poll)

    def _merge_passes(self, rows):
        """Добавляет новые проходы (по id события). Вызывается и по ходу долгой загрузки - данные видны сразу."""
        if not rows:
            return
        with self._events_lock:
            added = 0
            for ev in rows:
                if ev["id"] not in self._passes:
                    self._passes[ev["id"]] = ev
                    added += 1
            if added:
                self._passes_ready = True
                self.events_version += 1

    def _manual_counts(self):
        """Сколько раз сегодня вошли / вышли без идентификатора (кнопка вахтёра, команда оператора)."""
        with self._events_lock:
            manual = [e for e in self._passes.values() if e.get("method") in ("button", "operator")]
        return (sum(e["direction"] == "entrance" for e in manual), sum(e["direction"] == "exit" for e in manual))

    def _latest_pass(self):
        with self._events_lock:
            items = self._passes.values() if self._passes_ready else self._events
            last = max(items, key=lambda e: (e["ts"], e.get("id") or 0), default=None)
            return dict(last) if last else None

    @staticmethod
    def _pick(items, limit, inside):
        """items - по возрастанию времени. inside=True: только те, чей последний проход - вход (сейчас в здании)."""
        if inside:
            last = {}
            for e in items:
                if e.get("direction") in ("entrance", "exit") and e.get("user_id") is not None:
                    last[e["user_id"]] = e
            res = [e for e in last.values() if e["direction"] == "entrance"]
            res.sort(key=lambda e: e["ts"], reverse=True)
            return res
        return items[::-1][:limit]

    def list_events(self, limit=100, inside=False):
        """Проходы, новые первыми. inside=True - только находящиеся в здании сейчас.
        Источник - журнал Perco (все проходы, в т.ч. по картам); пока он недоступен - проходы, распознанные этой камерой."""
        try:
            limit = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            limit = 100
        with self._events_lock:
            if self._passes_ready:
                items = sorted(self._passes.values(), key=lambda e: (e["ts"], e["id"]))
            else:
                items = [{**e, "method": "face"} for e in self._events]
        return self._pick(items, limit, inside)

    # ================================================================== подразделения
    def _refresh_divisions(self):
        """Читает подразделения Perco. Сначала публикуется сам список (быстро), потом - кто в каком подразделении
        (может занять время, если API не отдаёт подразделение в списке сотрудников)."""
        with self._lock:
            cfg, conn, users = self.config, self.connection, dict(self.all_users)
        if cfg.get("source", "db") != "db" or conn is None:
            return
        try:
            divisions = conn.read_divisions()
        except Exception as e:
            log.warning("Cannot load divisions: %s", e)
            with self._lock:
                self.divisions, self.divisions_error = [], str(e)
                self.divisions_version += 1
            return
        with self._lock:
            self.divisions, self.divisions_error = divisions, None
            self.divisions_version += 1
        log.info("Divisions loaded: %d", len(divisions))
        try:
            _, mapping = load_divisions(cfg, conn, users.keys())
        except Exception as e:
            log.warning("Cannot load employees' divisions: %s", e)
            with self._lock:
                self.divisions_error = f"список подразделений есть, но не удалось определить подразделения сотрудников: {e}"
                self.divisions_version += 1
            return
        with self._lock:
            self.user_division = mapping
            self.divisions_version += 1
        log.info("Employee divisions loaded: %d of %d", len(mapping), len(users))

    def list_divisions(self):
        with self._lock:
            return list(self.divisions)

    # ================================================================== статус
    def play_path(self):
        return urlparse((self.config.get("rtsp_server") or {}).get("url", "")).path.strip("/")

    def status(self):
        cfg = self.config or {}
        rtsp = cfg.get("rtsp_server") or {}
        return {
            "state": self.state,
            "error": self.error,
            "mode": cfg.get("mode"),
            "source": cfg.get("source", "db"),
            "test_mode": bool(cfg.get("test_mode")),
            "camera_connected": bool(self.camera and self.camera.connected),
            "camera_fps": round(self.camera.fps, 1) if self.camera else 0,
            "loop_fps": round(self.loop_fps, 1),
            "users": max(0, len(self.users) - 1),
            "users_total": len(self.all_users),
            "embeddings": self.engine.embeddings_count if self.engine else 0,
            "stream_enabled": bool(rtsp.get("enabled")),
            "stream_encoder": self.streamer.encoder if self.streamer else None,
            "play_path": self.play_path() if rtsp.get("enabled") else None,
            "last_event": self.last_event,
            "events_version": self.events_version,
            "passes_source": "perco" if self._passes_ready else "local",
            "passes_error": self._passes_error,
            "passes_loading": self._passes_loading,
            "last_pass": self._latest_pass(),
            "manual_in": self._manual_counts()[0],
            "manual_out": self._manual_counts()[1],
            "divisions_version": self.divisions_version,
            "divisions_error": self.divisions_error,
        }

    # ================================================================== основной цикл
    def _loop(self, stop, engine, camera, connection, streamer, cfg):
        cam_cfg = cfg.get("camera", {})
        frames_dir = cam_cfg.get("frame_folder", "frame")
        keep_days = cam_cfg.get("frame_keep_days", 14)
        date_fmt = cfg.get("date_time_format", "%Y%m%d_%H%M%S")
        test_mode = bool(cfg.get("test_mode"))
        rtsp = cfg.get("rtsp_server") or {}
        stream_size = (int(rtsp.get("width", 1280)), int(rtsp.get("height", 720)))
        os.makedirs(frames_dir, exist_ok=True)

        last_no_signal = last_cleanup = 0.0
        last_day = datetime.now().day
        count, t0 = 0, time.monotonic()
        log.info("Capture loop started")

        while not stop.is_set():
            try:
                ret, frame = camera.video_capture()
                if not ret:
                    now = time.monotonic()
                    if streamer and not stop.is_set() and now - last_no_signal > 1.0:
                        streamer.put_frame(camera.no_signal_frame(stream_size))
                        last_no_signal = now
                    continue

                user_ids, locations = engine.detect_faces(camera.shrink(frame))
                locations = camera.to_full(locations)
                users = self.users
                open_n, door_state = camera.check_areas(locations, user_ids)
                camera.annotate(frame, locations, user_ids, users)

                if door_state != DoorState.CLOSED and open_n is not None:
                    uid = user_ids[open_n]
                    if connection.should_trigger(uid):
                        self._on_door(connection, uid, users.get(uid, "?"), door_state, frame,
                                      frames_dir, date_fmt, test_mode)
                if streamer:
                    streamer.put_frame(frame)

                count += 1
                now = time.monotonic()
                if now - t0 >= 5:
                    self.loop_fps, count, t0 = count / (now - t0), 0, now
                if now - last_cleanup > 3600:  # раз в час чистим старые снимки
                    last_cleanup = now
                    removed = cleanup_old_files(frames_dir, keep_days)
                    if removed:
                        log.info("Removed %d old snapshots", removed)
                if datetime.now().day != last_day:  # раз в сутки обновляем токен API
                    last_day = datetime.now().day
                    if not test_mode and cfg.get("connection"):
                        try:
                            connection.refresh_token()
                        except Exception as e:
                            log.error("Token refresh failed: %s", e)
            except Exception:
                log.exception("Error in capture loop")
                stop.wait(0.2)
        log.info("Capture loop stopped")

    def _on_door(self, connection, uid, name, door_state, frame, frames_dir, date_fmt, test_mode):
        label = "entrance" if door_state == DoorState.ENTRANCE else "exit"
        log.info("Door %s opened for %s (id %s)%s", label, name, uid, " [test mode]" if test_mode else "")
        self.last_event = {"time": datetime.now().strftime("%d.%m %H:%M:%S"), "user": str(name),
                           "direction": label, "test": test_mode}
        opened = None   # None - тест, турникет не открывался
        if not test_mode:
            opened = True
            try:
                connection.open_doors(uid, int(door_state), name)
            except Exception:
                opened = False
                log.exception("Cannot open the door")
        self._add_event({"ts": time.time(), "user": str(name), "user_id": uid, "direction": label,
                         "test": test_mode, "opened": opened})
        path = os.path.join(frames_dir, f"{datetime.now().strftime(date_fmt)}_Id{uid}_direct{int(door_state)}.jpg")
        try:
            cv2.imwrite(path, frame)
        except Exception:
            log.exception("Cannot save snapshot")
