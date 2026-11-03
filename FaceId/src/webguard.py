"""
Охрана веб-интерфейса (ASGI): стоит перед приложением FastAPI и пропускает запрос дальше, только если
  1. IP-адрес разрешён (web.allowed_ips; пусто = всем),
  2. есть действующая сессия (вход по логину и паролю; см. auth.py),
  3. уровень доступа сессии (роль оператора Perco) достаточен для запрошенного адреса:
       уровень 1 - «Камера и проходы»; 2 - + «Сотрудники и фото»; 3 - + «Конфиг» (и всё остальное).

Сама обрабатывает: GET /login, GET /login/info, POST /login, POST /logout, GET /api/me.
Остальное - в приложение. Без входа: страница (/) перенаправляется на /login, API отвечает 401.
"""
import asyncio
import json
from http.cookies import SimpleCookie
from pathlib import Path

from .auth import AuthManager
from .utils import ip_allowed

COOKIE = "fa_session"
MAX_BODY = 4096
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),          # страницу нельзя встроить в чужой сайт (на ней доступ к камере)
    (b"referrer-policy", b"same-origin"),
]

# какой уровень нужен для адреса; всё, чего нет в списках (конфиг, синхронизация и любые новые адреса), - уровень 3
LEVEL1_EXACT = {"/", "/api/status", "/api/events"}
LEVEL1_PREFIX = ("/webrtc/whep/",)
LEVEL2_EXACT = {"/api/users", "/api/divisions", "/api/photo", "/api/enable-pass", "/api/disable-pass"}


def required_level(path):
    if path in LEVEL1_EXACT or path.startswith(LEVEL1_PREFIX):
        return 1
    if path in LEVEL2_EXACT:
        return 2
    return 3


class WebGuard:
    def __init__(self, app, auth: AuthManager, get_config, login_page):
        self.app = app
        self.auth = auth
        self.get_config = get_config
        self.login_page = Path(login_page)

    # ------------------------------------------------------------------ ответы
    @staticmethod
    def _wrap(send):
        """Добавляет защитные заголовки и запрет кэширования ко всем ответам."""
        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                names = {k.lower() for k, _ in headers}
                headers += [h for h in SECURITY_HEADERS if h[0] not in names]
                if b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)
        return wrapped

    @staticmethod
    async def _respond(send, status, body, content_type, extra=()):
        headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode()), *extra]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send, status, obj, extra=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        await self._respond(send, status, body, b"application/json; charset=utf-8", extra)

    async def _redirect(self, send, location, extra=()):
        await self._respond(send, 303, b"", b"text/plain", [(b"location", location.encode()), *extra])

    # ------------------------------------------------------------------ cookie
    @staticmethod
    def _token(scope):
        for key, value in scope.get("headers") or []:
            if key == b"cookie":
                try:
                    jar = SimpleCookie()
                    jar.load(value.decode("latin-1"))
                    if COOKIE in jar:
                        return jar[COOKIE].value
                except Exception:
                    return None
        return None

    @staticmethod
    def _set_cookie(scope, token, max_age):
        parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if scope.get("scheme") == "https":
            parts.append("Secure")  # по HTTP куку с Secure браузер бы не принял
        return (b"set-cookie", "; ".join(parts).encode())

    # ------------------------------------------------------------------ основной вход
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = self._wrap(send)
        ip = (scope.get("client") or ("",))[0]
        rules = ((self.get_config() or {}).get("web") or {}).get("allowed_ips") or []
        if not ip_allowed(ip, rules):
            await self._json(send, 403, {"detail": "Forbidden"})
            return

        path, method = scope["path"], scope["method"]
        state = self.auth.state()
        token = self._token(scope)
        user = self.auth.session_user(token) if state == "ok" else None

        # ---- страницы и действия входа
        if path == "/login/info" and method == "GET":
            await self._json(send, 200, self.auth.info())
            return
        if path == "/login" and method == "GET":
            if state == "disabled" or user:
                await self._redirect(send, "/")
            else:
                await self._respond(send, 200, self.login_page.read_bytes(), b"text/html; charset=utf-8")
            return
        if path == "/login" and method == "POST":
            if state == "disabled":
                await self._json(send, 409, {"detail": "Вход отключён в конфиге (auth.enabled: false)"})
            else:
                await self._login(scope, receive, send, ip)
            return
        if path == "/logout" and method == "POST":
            self.auth.logout(token)
            await self._json(send, 200, {"message": "bye"}, [self._set_cookie(scope, "", 0)])
            return

        # ---- всё остальное: нужен вход
        if state == "unconfigured":
            if method == "GET" and path == "/":
                await self._redirect(send, "/login")  # на странице входа будет объяснение
            else:
                await self._json(send, 503, {"detail": "Вход в веб-интерфейс не настроен: " + "; ".join(self.auth.problems())})
            return
        if state == "ok" and user is None:
            if method == "GET" and (path == "/" or self._wants_html(scope)):
                await self._redirect(send, "/login")
            else:
                await self._json(send, 401, {"detail": "Требуется вход"})
            return

        # уровень доступа: при отключённом входе (auth.enabled: false) - полный
        level = self.auth.session_level(token) if state == "ok" else 3
        if path == "/api/me" and method == "GET":
            await self._json(send, 200, {"login": user, "auth": state != "disabled", "level": level})
            return
        if level < required_level(path):
            await self._json(send, 403, {"detail": "Недостаточно прав для этого действия"})
            return
        scope["auth_user"] = user  # кто вошёл (нужно, например, чтобы не дать потерять себе доступ при смене конфига)
        scope["auth_level"] = level
        await self.app(scope, receive, send)

    @staticmethod
    def _wants_html(scope):
        for key, value in scope.get("headers") or []:
            if key == b"accept":
                return b"text/html" in value
        return False

    async def _login(self, scope, receive, send, ip):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > MAX_BODY:
                await self._json(send, 413, {"detail": "Слишком большой запрос"})
                return
            if not message.get("more_body"):
                break
        try:
            data = json.loads(body or b"{}")
            login, password = data.get("login"), data.get("password")
        except (ValueError, AttributeError):
            await self._json(send, 400, {"detail": "Ожидается JSON: {login, password}"})
            return

        # проверка пароля может ждать ответа Perco до 8 секунд - в отдельном потоке, чтобы не вставали остальные запросы
        result = await asyncio.to_thread(self.auth.login, login, password, ip)
        if result.status == "ok":
            await self._json(send, 200, {"message": "ok", "login": result.message},
                             [self._set_cookie(scope, result.token, result.retry_after)])
        elif result.status == "locked":
            minutes = max(1, -(-result.retry_after // 60))
            await self._json(send, 429, {"detail": f"Слишком много неудачных попыток. Повторите через {minutes} мин."},
                             [(b"retry-after", str(result.retry_after).encode())])
        elif result.status == "forbidden":
            await self._json(send, 403, {"detail": result.message})
        elif result.status == "unavailable":
            await self._json(send, 503, {"detail": result.message})
        else:
            await asyncio.sleep(0.4)  # небольшая задержка замедляет перебор
            await self._json(send, 401, {"detail": result.message or "Неверный логин или пароль"})
