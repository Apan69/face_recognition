"""
Вход в веб-интерфейс. Логин и пароль проверяет Perco (раздел auth в config.yaml).

provider: perco (по умолчанию)
    Введённые на странице логин и пароль уходят в Perco тем же запросом, которым программа сама входит в Perco
    (POST /api/system/auth). Выдал токен - человек вошёл. Паролей в нашем конфиге нет, токен не сохраняется.
    allowed_logins - необязательный список учётных записей Perco, которым разрешён веб-интерфейс
    (пусто = любая, которую примет Perco). users - необязательные АВАРИЙНЫЕ учётные записи: действуют только
    пока Perco недоступен, чтобы можно было открыть страницу и исправить настройки.
provider: local
    Пользователи из auth.users (пароль открытым текстом или password_hash) - для установок без Perco.

Сессии лежат в памяти процесса (после перезапуска программы нужно войти заново). Подбор пароля: после max_attempts
неудач с одного IP или max_attempts_per_login неудач для одного логина (с любых адресов) блокировка на lockout_minutes.
Если раздел auth не настроен, веб закрыт целиком (распознавание и турникет работают); отключить вход можно только
явно: auth.enabled: false.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import namedtuple

from .utils import PercoError, check_perco_login

log = logging.getLogger("face_access.auth")

PLACEHOLDER = "ЗАМЕНИТЕ_НА_СВОЙ"  # пароль-заглушка: с ним вход заблокирован
HASH_PREFIX = "pbkdf2_sha256"
MAX_FIELD = 256                    # логин и пароль длиннее не принимаются
PROVIDERS = ("perco", "local")
MAX_TRACKED = 5000                 # сколько адресов/логинов помним для подсчёта неудач

# Уровни доступа по роли оператора Perco: 1 - «Камера и проходы»; 2 - + «Сотрудники и фото»; 3 - + «Конфиг».
# Роль не из списка - в веб-интерфейс не пускаем. Свои роли: auth.roles: {"Название роли": 1..3}
DEFAULT_ROLES = {"уровень1": 1, "уровень2": 2, "уровень3": 3, "admin": 3}

# status: ok | bad (неверные данные) | locked (заблокировано) | forbidden (Perco принял, но учётной записи
# нет в allowed_logins) | unavailable (вход не настроен или Perco недоступен)
LoginResult = namedtuple("LoginResult", "status token message retry_after")


def hash_password(password, iterations=240_000):
    """'pbkdf2_sha256$итерации$соль$хеш' - строка для password_hash в config.yaml."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{HASH_PREFIX}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _hash_ok(stored):
    try:
        prefix, iterations, salt, digest = stored.split("$")
        base64.b64decode(salt, validate=True)
        base64.b64decode(digest, validate=True)
        return prefix == HASH_PREFIX and int(iterations) > 0
    except Exception:
        return False


def _check_hash(stored, password):
    try:
        _, iterations, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except Exception:
        return False


def _norm_role(name):
    return str(name or "").strip().lower().replace(" ", "").replace("_", "")


def _eq(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))  # сравнение за постоянное время


class AuthManager:
    def __init__(self, get_config, clock=time.time, perco_check=None, role_lookup=None):
        self._get_config = get_config
        self._role_lookup = role_lookup  # login -> (имя роли, заблокирован) | None; None-функция - все получают уровень 3
        self._clock = clock
        self._perco_check = perco_check or check_perco_login
        self._lock = threading.Lock()
        self._sessions = {}  # токен -> {login, kind, fp, expires}
        self._fails = {}     # ключ ('ip:..' / 'login:..') -> [время неудач]
        self._locked = {}    # ключ -> до какого времени заблокирован

    # ------------------------------------------------------------------ настройки
    def _section(self, cfg=None):
        cfg = self._get_config() if cfg is None else cfg
        section = (cfg or {}).get("auth")
        return section if isinstance(section, dict) else None

    def _provider(self, section=None):
        section = self._section() if section is None else section
        return (section or {}).get("provider", "perco")

    def _perco_host(self, cfg=None):
        cfg = self._get_config() if cfg is None else cfg
        return str(((cfg or {}).get("connection") or {}).get("host") or "").strip()

    @staticmethod
    def _user_problems(users):
        out, seen = [], set()
        for n, u in enumerate(users, 1):
            if not isinstance(u, dict):
                out.append(f"auth.users[{n}]: ожидается login и password")
                continue
            login = u.get("login")
            if not isinstance(login, str) or not login.strip():
                out.append(f"auth.users[{n}]: login должен быть непустой строкой (возьмите значение в кавычки)")
                continue
            if login in seen:
                out.append(f"auth.users: логин {login!r} повторяется")
            seen.add(login)
            password, phash = u.get("password"), u.get("password_hash")
            if phash is not None:
                if not isinstance(phash, str) or not _hash_ok(phash):
                    out.append(f"{login}: password_hash неверного формата (получите его командой python3 run.py --hash-password)")
            elif not isinstance(password, str) or not password:
                out.append(f"{login}: нужен password (строка, лучше в кавычках) или password_hash")
            elif password == PLACEHOLDER:
                out.append(f"{login}: замените пароль-заглушку {PLACEHOLDER}")
        return out

    def problems(self, cfg=None, user=None):
        """Что мешает входу: [] - всё настроено (или вход явно отключён).
        user - логин того, кто сейчас меняет конфиг: проверяем, что он не потеряет доступ."""
        full = self._get_config() if cfg is None else cfg
        section = self._section(full)
        if section is None:
            return ["в config.yaml нет раздела auth"]
        if section.get("enabled") is False:
            return []
        provider = section.get("provider", "perco")
        if provider not in PROVIDERS:
            return [f"auth.provider: допустимо perco или local, указано {provider!r}"]
        users = section.get("users")
        out = []
        if provider == "perco":
            if not self._perco_host(full):
                out.append("auth.provider: perco требует connection.host (адрес Perco)")
            allowed = section.get("allowed_logins")
            if allowed is not None and (not isinstance(allowed, list) or not all(isinstance(x, str) and x.strip() for x in allowed)):
                out.append("auth.allowed_logins должен быть списком строк")
            elif allowed and user is not None and user.lower() not in [x.lower() for x in allowed]:
                out.append(f"ваша учётная запись {user} не входит в auth.allowed_logins - вы потеряли бы доступ")
            if users is not None:  # аварийные учётные записи необязательны, но если есть - должны быть корректны
                out += self._user_problems(users) if isinstance(users, list) else ["auth.users должен быть списком"]
        else:
            if not isinstance(users, list) or not users:
                return ["в разделе auth нет ни одного пользователя (auth.users)"]
            out += self._user_problems(users)
            if user is not None and not out and user not in [u.get("login") for u in users if isinstance(u, dict)]:
                out.append(f"ваш логин {user} не найден в auth.users - вы потеряли бы доступ")
        return out

    def state(self):
        """disabled - вход отключён явно; ok - вход работает; unconfigured - веб закрыт, пока не настроят auth."""
        section = self._section()
        if section is not None and section.get("enabled") is False:
            return "disabled"
        return "unconfigured" if self.problems() else "ok"

    def info(self):
        state = self.state()
        return {"state": state, "provider": self._provider(),
                "message": "; ".join(self.problems()) if state == "unconfigured" else ""}

    def _users(self):
        section = self._section() or {}
        users = section.get("users")
        return [u for u in users if isinstance(u, dict) and isinstance(u.get("login"), str)] if isinstance(users, list) else []

    def _allowed(self, login):
        allowed = (self._section() or {}).get("allowed_logins")
        if not isinstance(allowed, list) or not allowed:
            return True
        return login.strip().lower() in [str(x).strip().lower() for x in allowed]

    @staticmethod
    def _local_fp(user):
        raw = "\0".join([user["login"], str(user.get("password") or ""), str(user.get("password_hash") or "")])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def role_level(self, role_name):
        table = dict(DEFAULT_ROLES)
        custom = (self._section() or {}).get("roles")
        if isinstance(custom, dict):
            table.update({_norm_role(k): v for k, v in custom.items() if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3})
        return table.get(_norm_role(role_name), 0)

    @staticmethod
    def _local_level(user):
        lvl = user.get("level", 3)
        return lvl if isinstance(lvl, int) and not isinstance(lvl, bool) and 1 <= lvl <= 3 else 3

    def _perco_fp(self, login):
        return hashlib.sha256(f"perco\0{login.strip().lower()}\0{self._perco_host()}".encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ проверка пароля
    def _match_local(self, login, password):
        found = None
        for u in self._users():  # перебираем всех без раннего выхода: время ответа не выдаёт, есть ли такой логин
            login_ok = _eq(u["login"], login)
            if u.get("password_hash") is not None:
                password_ok = _check_hash(str(u["password_hash"]), password)
            else:
                password_ok = _eq(str(u.get("password") or ""), password)
            if login_ok and password_ok:
                found = u
        return found

    def _perco_verify(self, login, password):
        """-> ('ok' | 'bad' | 'down')"""
        try:
            return "ok" if self._perco_check(self._perco_host(), login, password) else "bad"
        except PercoError:
            return "down"

    # ------------------------------------------------------------------ защита от подбора
    def _limits(self, section):
        return (max(1, int(section.get("max_attempts", 5))), max(1, int(section.get("max_attempts_per_login", 10))),
                float(section.get("lockout_minutes", 10)) * 60)

    @staticmethod
    def _keys(ip, login):
        return [f"ip:{ip}", f"login:{login.strip().lower()[:64]}"]

    def _register_fail(self, ip, login, now, section):
        per_ip, per_login, window = self._limits(section)
        for key, limit in zip(self._keys(ip, login), (per_ip, per_login)):
            fails = [t for t in self._fails.get(key, []) if now - t < window] + [now]
            if len(fails) >= limit:
                self._locked[key] = now + window
                self._fails.pop(key, None)
                log.warning("Web login: %s заблокирован на %d мин после %d неудачных попыток", key, window // 60, limit)
            else:
                self._fails[key] = fails
        if len(self._fails) + len(self._locked) > MAX_TRACKED:  # перебор разных логинов не раздувает память
            self._fails = {k: v for k, v in self._fails.items() if now - v[-1] < window}
            self._locked = {k: t for k, t in self._locked.items() if t > now}
            if len(self._fails) > MAX_TRACKED:  # всё ещё много: оставляем самые свежие
                self._fails = dict(sorted(self._fails.items(), key=lambda kv: kv[1][-1])[-MAX_TRACKED:])
            if len(self._locked) > MAX_TRACKED:
                self._locked = dict(sorted(self._locked.items(), key=lambda kv: kv[1])[-MAX_TRACKED:])

    def _locked_for(self, ip, login, now):
        until = 0
        for key in self._keys(ip, login):
            t = self._locked.get(key, 0)
            if t > now:
                until = max(until, t)
            else:
                self._locked.pop(key, None)
        return until

    # ------------------------------------------------------------------ вход
    def login(self, login, password, ip):
        if self.state() != "ok":
            return LoginResult("unavailable", None, "Вход в веб-интерфейс не настроен: " + "; ".join(self.problems()), 0)
        section = self._section() or {}
        provider = self._provider(section)
        now = self._clock()
        valid_input = (isinstance(login, str) and isinstance(password, str) and login.strip() and password
                       and len(login) <= MAX_FIELD and len(password) <= MAX_FIELD)
        key_login = login if isinstance(login, str) else ""
        with self._lock:
            until = self._locked_for(ip, key_login, now)
            if until:
                return LoginResult("locked", None, "", int(until - now) + 1)

        kind, who, note, level = None, None, "", 3
        if valid_input:
            if provider == "perco":
                verdict = self._perco_verify(login, password)
                if verdict == "ok":
                    if not self._allowed(login):
                        log.warning("Web login: учётная запись Perco %r принята, но не входит в allowed_logins (%s)", login[:40], ip)
                        return LoginResult("forbidden", None, "Эта учётная запись Perco не допущена к веб-интерфейсу", 0)
                    if self._role_lookup is not None:
                        try:
                            found = self._role_lookup(login.strip())
                        except Exception as e:
                            log.warning("Web login: не удалось получить роль оператора %r: %s", login[:40], e)
                            return LoginResult("unavailable", None, "Perco принял пароль, но не удалось определить роль оператора. Повторите позже", 0)
                        if found is None:
                            return LoginResult("forbidden", None, "Учётная запись не найдена в списке операторов Perco", 0)
                        role, blocked = found
                        if blocked:
                            return LoginResult("forbidden", None, "Учётная запись оператора заблокирована в Perco", 0)
                        level = self.role_level(role)
                        if level == 0:
                            log.warning("Web login: роль %r оператора %r не допущена к веб-интерфейсу", role, login[:40])
                            return LoginResult("forbidden", None, f"Роль «{role}» не допущена к веб-интерфейсу", 0)
                    kind, who = "perco", login.strip()
                elif verdict == "down":
                    user = self._match_local(login, password) if self._users() else None
                    if user:
                        kind, who, level = "local", user["login"], self._local_level(user)
                        log.warning("Web login: АВАРИЙНЫЙ вход %s (Perco недоступен) с %s", who, ip)
                    elif not self._users():
                        return LoginResult("unavailable", None, "Не удалось связаться с Perco - проверьте связь и connection.host", 0)
                    else:
                        note = "Не удалось связаться с Perco, а аварийный логин или пароль не подошли"
            else:
                user = self._match_local(login, password)
                if user:
                    kind, who, level = "local", user["login"], self._local_level(user)

        with self._lock:
            if kind is None:
                self._register_fail(ip, key_login, now, section)
                log.warning("Web login failed from %s (login %r)", ip, str(login)[:40])
                return LoginResult("bad", None, note, 0)
            for key in self._keys(ip, who):
                self._fails.pop(key, None)
            hours = float(section.get("session_hours", 12))
            token = secrets.token_urlsafe(32)
            fp = self._perco_fp(who) if kind == "perco" else self._local_fp(self._match_local_entry(who))
            self._sessions[token] = {"login": who, "kind": kind, "fp": fp, "level": level, "expires": now + hours * 3600}
            for t in [t for t, s in self._sessions.items() if s["expires"] <= now]:
                del self._sessions[t]
        log.info("Web login: %s (%s) from %s", who, kind, ip)
        return LoginResult("ok", token, who, int(hours * 3600))

    def _match_local_entry(self, login):
        return next(u for u in self._users() if u["login"] == login)

    # ------------------------------------------------------------------ сессии
    def session_user(self, token):
        """Логин по токену сессии или None (нет сессии, истекла, учётные данные или допуск в конфиге изменились)."""
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
        if s is None:
            return None
        if s["expires"] <= self._clock():
            self.logout(token)
            return None
        if s["kind"] == "perco":
            valid = (self._provider() == "perco" and self._allowed(s["login"])
                     and hmac.compare_digest(self._perco_fp(s["login"]), s["fp"]))
        else:
            user = next((u for u in self._users() if _eq(u["login"], s["login"])), None)
            valid = user is not None and hmac.compare_digest(self._local_fp(user), s["fp"])
        if not valid:
            self.logout(token)
            return None
        return s["login"]

    def session_level(self, token):
        """Уровень доступа сессии: 1 - только «Камера и проходы», 2 - + «Сотрудники и фото», 3 - всё. 0 - нет сессии."""
        with self._lock:
            s = self._sessions.get(token)
        return int(s.get("level", 1)) if s else 0

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token, None)
