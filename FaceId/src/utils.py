"""Общие утилиты: логирование, связь со СКУД (API турникета), загрузка пользователей."""
import calendar
import ipaddress
import logging
import os
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from enum import IntEnum
from logging.handlers import RotatingFileHandler

import pandas as pd
import requests

log = logging.getLogger("face_access.utils")


class PercoError(Exception):
    """Ошибка обращения к API системы допуска (Perco)."""


class DoorState(IntEnum):
    CLOSED = 0
    ENTRANCE = 1  # лицо в area_2
    EXIT = 2      # лицо в area_1


def setup_logger(config):
    """Лог в файл (с ротацией 5 МБ x 5) и в консоль/journald."""
    logger = logging.getLogger("face_access")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%d/%m/%y %H:%M:%S")
    fh = RotatingFileHandler(config.get("log_file", "log.txt"), maxBytes=5 * 1024 * 1024,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


class Connection:
    """API системы допуска (Perco): авторизация, список сотрудников, открытие турникета."""
    JSON_HEADERS = {"Content-type": "application/json; charset=UTF-8"}

    def __init__(self, config):
        conn = config.get("connection") or {}
        self.host = conn.get("host")
        self.login = conn.get("login")
        self.password = conn.get("password")
        # параметры запроса ПОЛНОГО списка сотрудников для веб-страницы (по умолчанию запрос без параметров)
        self.all_users_query = conn.get("all_users_query") or {}
        tur = config["turnstiles"]
        self.turnstile_id = tur.get("id_tur")
        self.face_pass_field_id = int(tur.get("face_pass_field_id", -4))  # поле карточки Perco для отметки прохода по лицу
        self.min_time_diff = float(tur.get("min_time_diff", tur.get("interval", 6)))
        self._session = requests.Session()
        self._token = None
        self._token_lock = threading.Lock()
        self._debounce_lock = threading.Lock()
        self._last_id = None
        self._last_time = 0.0

    def _url(self, path):
        return f"http://{self.host}{path}"

    def refresh_token(self):
        with self._token_lock:
            r = self._session.post(
                self._url("/api/system/auth"),
                json={"login": self.login, "password": self.password},
                headers={**self.JSON_HEADERS, "Authorization": "Bearer null"}, timeout=10)
            r.raise_for_status()
            self._token = r.json()["token"]
            log.info("Authorization token refreshed")

    def _headers(self):
        if self._token is None:
            self.refresh_token()
        token = self._token if self._token.lower().startswith("bearer ") else "Bearer " + self._token
        return {**self.JSON_HEADERS, "Authorization": token}

    def _get_json(self, path, params=None):
        """GET к API с одной повторной попыткой при устаревшем токене."""
        for attempt in (1, 2):
            r = self._session.get(self._url(path), headers=self._headers(), params=params or None, timeout=15)
            if r.status_code in (401, 403) and attempt == 1:
                self._token = None
                continue
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _row_ts(r):
        """Время события Perco в секундах (epoch). UTC надёжнее: не зависит от часового пояса сервера."""
        for key, utc in (("time_label_utc", True), ("time_label", False)):
            value = r.get(key)
            if value:
                try:
                    t = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
                    return calendar.timegm(t.timetuple()) if utc else time.mktime(t.timetuple())
                except ValueError:
                    continue
        return None

    @staticmethod
    def _pass_event(r, outside_zone_id):
        """Строка журнала Perco (событие «Проход по идентификатору») -> запись для страницы."""
        ts = Connection._row_ts(r)
        if ts is None:
            return None
        zone_exit, zone_enter = r.get("zone_exit_id"), r.get("zone_enter_id")
        if zone_exit == outside_zone_id:
            direction = "entrance"      # шёл с неконтролируемой территории -> вошёл
        elif zone_enter == outside_zone_id:
            direction = "exit"
        else:
            direction = "other"
        return {"id": r.get("id"), "ts": ts, "user": (r.get("fio") or "").strip() or "?", "user_id": r.get("user_id"),
                "tabel": r.get("tabel_number"), "division": r.get("division_name"), "direction": direction,
                "method": "card" if r.get("identifier") else "face", "test": False, "opened": True}

    @staticmethod
    def _manual_events(remote, commands, own_login, reader_directions=None, outside_zone_id=1):
        """Проходы без идентификатора: событие «Проход по команде от ДУ» (112) - турникет провернулся по кнопке
        вахтёра или по команде оператора. Направление - по считывателю, у которого записано событие
        (reader_directions, по умолчанию 1 - вход, 2 - выход), либо по зонам, если Perco их указал.
        commands: [(device_id, время, оператор)] - команды «Открыть ИУ»: если рядом есть команда оператора, показываем его."""
        out = []
        rd = reader_directions or {1: "entrance", 2: "exit"}
        for r in remote:
            ts = Connection._row_ts(r)
            if ts is None or r.get("id") is None:
                continue
            if r.get("zone_exit_id") == outside_zone_id:
                direction = "entrance"
            elif r.get("zone_enter_id") == outside_zone_id:
                direction = "exit"
            else:
                direction = rd.get(r.get("resource_number"), "other")
            dev = r.get("device_id")
            by = [op for d, t, op in commands if d == dev and abs(t - ts) <= 5 and op]
            if by:
                user, method = f"Оператор: {by[0]}", "operator"
            else:
                user, method = "Вахтёр (кнопка)", "button"
            out.append({"id": r["id"], "ts": ts, "user": user, "user_id": None, "tabel": None, "division": None,
                        "direction": direction, "method": method, "test": False, "opened": True})
        return out

    def read_pass_events(self, begin, end, event_ids=(17,), outside_zone_id=1, on_page=None,
                         remote_event_id=112, command_event_id=16010, reader_directions=None):
        """Проходы всех за период [begin, end] - datetime: по картам и лицу (события из event_ids) и без идентификатора
        (кнопка вахтёра, команда оператора - событие remote_event_id). Записи - в формате журнала страницы.
        on_page(список) вызывается после каждой страницы - чтобы показывать данные, не дожидаясь конца загрузки."""
        base = {"beginDatetime": begin.strftime("%Y-%m-%d %H:%M"), "endDatetime": end.strftime("%Y-%m-%d %H:%M")}
        ids = {int(i) for i in event_ids}
        out, seen, page = [], set(), 1
        remote, commands = [], []
        while page <= 400:
            params = dict(base, page=page) if page > 1 else base
            data = self._get_json("/api/eventsystem", params)
            rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
            fresh = [r for r in rows if r.get("id") not in seen]
            if not fresh:               # пусто или API игнорирует page и отдаёт то же самое
                break
            got = []
            for r in fresh:
                seen.add(r.get("id"))
                eid = r.get("event_name_id")
                if eid in ids:
                    ev = self._pass_event(r, outside_zone_id)
                    if ev and ev["id"] is not None:
                        got.append(ev)
                elif eid == remote_event_id:
                    remote.append(r)
                elif eid == command_event_id:
                    ts = self._row_ts(r)
                    if ts is not None:
                        commands.append((r.get("device_id"), ts, r.get("user_name")))
            out.extend(got)
            if on_page:
                on_page(got)
            pages = int(data.get("total") or 1) if isinstance(data, dict) else 1
            if page >= pages:
                break
            page += 1
        out.extend(self._manual_events(remote, commands, self.login, reader_directions, outside_zone_id))
        return out

    def read_operators(self):
        """Операторы Perco: {логин в нижнем регистре: (имя роли, заблокирован)}. Нужны, чтобы по роли решить,
        какие вкладки веб-интерфейса доступны вошедшему."""
        out, page = {}, 1
        while page <= 50:
            data = self._get_json("/api/users/operators/table", {"page": page} if page > 1 else None)
            rows = data.get("rows", []) if isinstance(data, dict) else (data or [])
            for r in rows:
                login = str(r.get("login") or "").strip().lower()
                if login:
                    out[login] = (str(r.get("role_name") or ""), bool(r.get("is_block")))
            pages = int(data.get("total") or 1) if isinstance(data, dict) else 1
            if page >= pages or not rows:
                break
            page += 1
        return out

    @staticmethod
    def _division_id(value):
        """Подразделение в любом из видов -> id: {"2": "Название"} | {"id": 2, ...} | 2 | "2"."""
        if isinstance(value, dict) and value:
            value = value["id"] if "id" in value else next(iter(value))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def read_staff(self, enabled_only=True):
        """{id: (имя, id подразделения или None)}. enabled_only=True: только сотрудники с включённым проходом
        по лицу (им камера открывает турникет); False: полный список (для веб-страницы).
        Подразделение берётся из списка, если API отдаёт его там."""
        params = {"withPhone": "true"} if enabled_only else dict(self.all_users_query)
        out = {}
        for u in self._get_json("/api/users/staff/list", params):
            division = None
            for key in ("division", "division_id", "divisionId"):
                if u.get(key) not in (None, "", {}):
                    division = self._division_id(u[key])
                    if division is not None:
                        break
            out[int(u["id"])] = (u["name"], division)
        return out

    def read_users(self, enabled_only=True):
        """{id: имя}, см. read_staff()."""
        return {uid: v[0] for uid, v in self.read_staff(enabled_only).items()}

    def read_divisions(self):
        """Подразделения Perco: [{id, name, parent_id}] (удалённые пропускаются)."""
        rows = []
        for d in self._get_json("/api/divisions/list"):
            if d.get("is_removed"):
                continue
            parent = d.get("parent_id")
            rows.append({"id": int(d["id"]), "name": str(d.get("name") or ""),
                         "parent_id": int(parent) if parent not in (None, "") else None})
        return rows

    def read_staff_divisions(self, ids, workers=6):
        """{id сотрудника: id подразделения} по карточкам - на случай, когда список подразделение не отдаёт.
        Запросы идут параллельно, при устаревшем токене он обновляется и повторяется один раз."""
        result, ids = {}, list(ids)

        def fetch(uid, headers):
            try:
                r = requests.get(self._url(f"/api/users/staff/{int(uid)}"), headers=headers, timeout=15)
                if r.status_code in (401, 403):
                    return uid, None, True
                if r.status_code >= 300:
                    return uid, None, False
                return uid, self._division_id(r.json().get("division")), False
            except (requests.RequestException, ValueError, AttributeError):
                return uid, None, False

        for attempt in (1, 2):
            if not ids:
                break
            headers = self._headers()
            auth_failed = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for uid, division, denied in pool.map(lambda i: fetch(i, headers), ids):
                    if denied:
                        auth_failed.append(uid)
                    elif division is not None:
                        result[uid] = division
            if not auth_failed or attempt == 2:
                break
            self._token = None
            ids = auth_failed
        return result

    # ------------------------------------------------------------------ «проход по лицу» в Perco
    # Единственное место в проекте, знающее, как это устроено в Perco: для отметки используется служебное
    # поле карточки сотрудника; его id задан в config.yaml (turnstiles.face_pass_field_id, обычно -4: в интерфейсе
    # Perco оно называется «Телефон» и нигде больше не задействовано).
    #   проход включён  - в поле записан id сотрудника;   выключен - поле пустое.
    # Список тех, кому камера открывает турникет, - выборка staff/list с параметром withPhone=true
    # (сотрудники с заполненным полем), см. read_users().
    def _write_face_pass_field(self, user_id, value):
        url = self._url(f"/api/users/staff/{int(user_id)}")
        payload = {"additional_fields": {"text": [{"id": self.face_pass_field_id, "text": str(value)}]}}
        for attempt in (1, 2):
            try:
                r = self._session.post(url, json=payload, headers=self._headers(), timeout=15)
            except requests.RequestException as e:
                raise PercoError(f"нет связи с Perco: {e}")
            if r.status_code in (401, 403) and attempt == 1:
                self._token = None  # токен устарел - получить новый и повторить
                continue
            break
        if r.status_code >= 300:
            raise PercoError(f"Perco ответил {r.status_code}: {r.text[:200]}")
        # проверка чтением карточки: значение действительно записалось
        try:
            card = self._session.get(url, headers=self._headers(), timeout=15).json()
            fields = (card.get("additional_fields") or {}).get("text") or []
            current = next((f.get("text") for f in fields if f.get("id") == self.face_pass_field_id), None)
        except (requests.RequestException, ValueError, AttributeError) as e:
            log.warning("Cannot verify the record of staff id %s: %s", user_id, e)
            return
        if current is None:
            raise PercoError(f"в карточке сотрудника нет поля id {self.face_pass_field_id} (turnstiles.face_pass_field_id)")
        if str(current) != str(value):
            raise PercoError(f"значение не записалось (в карточке: {current!r})")

    def enable_face_pass(self, user_id):
        """Включить проход по лицу: записать в карточку id сотрудника."""
        self._write_face_pass_field(user_id, int(user_id))
        log.info("Face pass enabled in Perco for id %s", user_id)

    def disable_face_pass(self, user_id):
        """Выключить проход по лицу: очистить поле в карточке."""
        self._write_face_pass_field(user_id, "")
        log.info("Face pass disabled in Perco for id %s", user_id)

    def should_trigger(self, user_id):
        """Защита от повторов: тот же человек не чаще, чем раз в min_time_diff секунд."""
        now = time.monotonic()
        with self._debounce_lock:
            if user_id != self._last_id or now - self._last_time > self.min_time_diff:
                self._last_id, self._last_time = user_id, now
                return True
            return False

    def open_doors(self, user_id, direction, user_name):
        payload = {"user_id": int(user_id), "direction": int(direction),
                   "event_description": f"camera {int(direction)}"}
        url = self._url(f"/api/devices/{self.turnstile_id}/pass")
        for attempt in (1, 2):
            result = None
            try:
                r = self._session.post(url, json=payload, headers=self._headers(), timeout=10)
                result = r.json().get("result")
            except (requests.RequestException, ValueError, KeyError) as e:
                log.error("Turnstile request failed: %s", e)
            if result == "ok":
                return True
            log.warning("Turnstile refused (result=%s), refreshing token (attempt %d)", result, attempt)
            self._token = None
        log.error("Turnstile did not open for %s", user_name)
        return False


def check_perco_login(host, login, password, timeout=8):
    """Проверка логина и пароля учётной записи Perco - тем же запросом, которым программа сама входит в Perco.
    True - Perco выдал токен; False - отказал (любой ответ без токена); PercoError - Perco недоступен или сбоит.
    Токен нигде не сохраняется: нужен только факт, что Perco учётную запись принял."""
    try:
        r = requests.post(f"http://{host}/api/system/auth", json={"login": login, "password": password},
                          headers={**Connection.JSON_HEADERS, "Authorization": "Bearer null"}, timeout=timeout)
    except requests.RequestException as e:
        log.warning("Perco login check: no connection (%s)", e.__class__.__name__)
        raise PercoError("нет связи с Perco")
    if r.status_code >= 500:
        log.warning("Perco login check: server error %s", r.status_code)
        raise PercoError(f"Perco ответил ошибкой {r.status_code}")
    if r.status_code >= 300:
        return False
    try:
        return bool(r.json().get("token"))
    except (ValueError, AttributeError):
        return False


def load_users(config, connection=None):
    """{id: имя}; id 0 - 'не опознан'."""
    if config.get("source", "db") == "excel":
        df = pd.read_excel(config["excel_file"])
        users = {int(i): str(n) for i, n in df.iloc[:, :2].values}
    else:
        users = connection.read_users()
    users[0] = config.get("no_name_user", "unknown")
    return users


def load_all_users(config, users, connection=None):
    """Все сотрудники для веб-страницы {id: имя}. Камерой распознаются только те, кто есть в users."""
    recognized = {k: v for k, v in users.items() if k != 0}
    if config.get("source", "db") == "excel" or connection is None:
        return recognized
    try:
        everyone = connection.read_users(enabled_only=False)
    except Exception as e:
        log.warning("Cannot load the full staff list (%s) - the web page will show recognized users only", e)
        everyone = {}
    everyone.update(recognized)
    return everyone


def load_divisions(config, connection, user_ids):
    """(подразделения, {id сотрудника: id подразделения}). Только для источника db; при ошибке - исключение."""
    if config.get("source", "db") != "db" or connection is None:
        return [], {}
    divisions = connection.read_divisions()
    ids = [i for i in user_ids if i != 0]
    staff = connection.read_staff(enabled_only=False)
    if any(div is not None for _, div in staff.values()):  # список сам отдаёт подразделение
        mapping = {uid: staff[uid][1] for uid in ids if uid in staff and staff[uid][1] is not None}
    else:  # не отдаёт - берём из карточек сотрудников
        mapping = connection.read_staff_divisions(ids)
    return divisions, mapping


def cleanup_old_files(folder, keep_days):
    """Удаляет файлы старше keep_days суток (снимки срабатываний)."""
    if not keep_days or not os.path.isdir(folder):
        return 0
    limit = time.time() - float(keep_days) * 86400
    removed = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < limit:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def ip_allowed(ip, rules):
    """Пустой список правил = разрешено всем. Правила: IP или сети ('192.168.0.0/24')."""
    if not rules:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped:
        addr = mapped
    if addr.is_loopback:
        return True
    for rule in rules:
        try:
            if addr in ipaddress.ip_network(str(rule), strict=False):
                return True
        except ValueError:
            continue
    return False
