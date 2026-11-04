"""Веб-интерфейс (FastAPI): настройка параметров, просмотр потока из MediaMTX, добавление фото сотрудников."""
import copy
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from src.auth import AuthManager
from src.config import apply_runtime_secrets, parse_config
from src.logview import read_log
from src.service import AccessService
from src.utils import Connection
from src.webguard import WebGuard

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = os.environ.get("FACE_CONFIG", str(BASE_DIR / "configs" / "config.yaml"))
MAX_PHOTO_BYTES = 15 * 1024 * 1024

service = AccessService(CONFIG_FILE)

_raw = {"mtime": None, "data": {}}


def _raw_config():
    """Конфиг без проверки. Нужен охране, чтобы пускать на страницу и тогда, когда сервис не запустился
    из-за ошибки в конфиге (иначе исправить его через браузер было бы нельзя)."""
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if _raw["mtime"] != mtime:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                _raw.update(mtime=mtime, data=yaml.safe_load(f) or {})
        return _raw["data"] if isinstance(_raw["data"], dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def guard_config():
    return service.config or _raw_config()


def operator_role(login):
    """(имя роли, заблокирован) оператора Perco по логину или None, если такого оператора нет.
    Роль определяет, какие вкладки доступны (Уровень1 / Уровень2 / Уровень3 и admin)."""
    # сервис мог не запуститься - тогда отдельное подключение; пароль Perco - из аргумента запуска
    conn = service.connection or Connection(apply_runtime_secrets(copy.deepcopy(guard_config())))
    return conn.read_operators().get(login.strip().lower())


auth = AuthManager(guard_config, role_lookup=operator_role)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service.start_async()  # модель грузится в фоне, страница доступна сразу
    try:
        yield
    finally:
        service.stop()


# автодокументацию FastAPI (/docs) отключаем: лишняя открытая поверхность
app = FastAPI(lifespan=lifespan, title="Face Access", docs_url=None, redoc_url=None, openapi_url=None)

# охрана: сначала IP-адрес (web.allowed_ips), затем вход по логину и паролю (auth в config.yaml)
app.add_middleware(WebGuard, auth=auth, get_config=guard_config,
                   login_page=BASE_DIR / "templates" / "login.html")


STATIC_FILES = {"camera.css", "camera.js"}   # только эти файлы страницы; охрана пускает к ним лишь после входа


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "templates" / "camera.html")


@app.get("/static/{name}")
def static_file(name: str):
    if name not in STATIC_FILES:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(BASE_DIR / "templates" / "static" / name)


@app.get("/api/log")
def api_log(errors: bool = False):
    """Записи log.txt за последние log_view_hours часов (config.yaml); errors=1 - только WARNING и выше."""
    cfg = service.config or _raw_config()
    try:
        hours = float(cfg.get("log_view_hours", 6))
    except (TypeError, ValueError):
        hours = 6.0
    hours = min(max(hours, 0.1), 720.0)
    lines, truncated = read_log(cfg.get("log_file", "log.txt"), hours, errors)
    shown = int(hours) if hours == int(hours) else round(hours, 1)
    if lines is None:
        return {"hours": shown, "lines": [], "truncated": False, "error": "файл журнала не найден"}
    return {"hours": shown, "lines": lines, "truncated": truncated}


@app.get("/api/status")
def status():
    return service.status()


@app.get("/api/users")
def users():
    return service.list_users()


@app.get("/api/events")
def events(limit: int = 100, inside: bool = False):
    """Журнал проходов («кто входил и выходил»), новые сверху. inside=1 - только находящиеся в здании сейчас."""
    return service.list_events(max(1, min(limit, 3000)), inside)


@app.get("/api/divisions")
def divisions():
    """Подразделения Perco для фильтра списка сотрудников (пусто, если источник - Excel или API недоступен)."""
    return service.list_divisions()


def _apply_config(raw: bytes, user=None):
    """Проверить и применить конфиг (при ошибке остаётся прежний)."""
    try:
        problems = auth.problems(parse_config(raw), user)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid config: {e}")
    if problems:  # не даём сами себя отрезать от веб-интерфейса
        raise HTTPException(status_code=422, detail="В новом конфиге не настроен вход в веб-интерфейс (" +
                            "; ".join(problems) + "): после применения вы потеряли бы доступ. Исправьте раздел auth.")
    try:
        service.apply_config(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid config: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Config rejected, previous one restored: {e}")


@app.get("/api/config")
def get_config_text():
    """Текущий configs/config.yaml для редактирования на странице."""
    try:
        text = Path(CONFIG_FILE).read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read config: {e}")
    return Response(content=text, media_type="text/plain; charset=utf-8")


@app.put("/api/config")
async def put_config_text(request: Request):
    """Сохранить и применить конфиг, отредактированный на странице."""
    raw = await request.body()
    await run_in_threadpool(_apply_config, raw, request.scope.get("auth_user"))
    return {"message": "Config updated"}


@app.post("/config")
def set_config(request: Request, config_file: UploadFile = File(...)):
    """Загрузка файла config.yaml."""
    _apply_config(config_file.file.read(), request.scope.get("auth_user"))
    return {"filename": config_file.filename, "message": "Config updated"}


@app.post("/sync")
def sync():
    """Перечитать пользователей и досчитать эмбеддинги для новых фото."""
    try:
        return {"message": "Data synchronized", **service.sync()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@app.post("/api/photo")
def add_photo(user_id: int = Form(...), photo: UploadFile = File(...)):
    """Новое фото сотрудника (снимок веб-камеры браузера или файл)."""
    data = photo.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Файл слишком большой (максимум 15 МБ)")
    try:
        return {"message": "Photo saved", **service.add_photo(user_id, data)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {e}")


def _pass_errors(call):
    """Единое преобразование ошибок для кнопок «Включить/Убрать проход по лицу»."""
    try:
        return call()
    except KeyError:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {e}")


@app.post("/api/enable-pass")
def enable_pass(user_id: int = Form(...), photo: Optional[UploadFile] = File(None)):
    """Включить проход по лицу: эмбеддинг по фото + отметка в Perco."""
    data = photo.file.read(MAX_PHOTO_BYTES + 1) if photo else None
    if data is not None and len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Файл слишком большой (максимум 15 МБ)")
    return _pass_errors(lambda: {"message": "Face pass enabled", **service.enable_pass(user_id, data or None)})


@app.post("/api/disable-pass")
def disable_pass(user_id: int = Form(...)):
    """Убрать проход по лицу: фото и эмбеддинг - в архив, отметка в Perco снимается."""
    return _pass_errors(lambda: {"message": "Face pass disabled", **service.disable_pass(user_id)})


@app.post("/webrtc/whep/{name}")
async def whep(name: str, request: Request):
    """Прокси WebRTC-просмотра: страница получает поток из MediaMTX тем же адресом/протоколом, что и сама."""
    if not name or name != service.play_path():
        raise HTTPException(status_code=404, detail="Unknown stream")
    base = ((service.config.get("webrtc") or {}).get("api") or "http://127.0.0.1:8889").rstrip("/")
    sdp = await request.body()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{base}/{name}/whep", content=sdp, headers={"Content-Type": "application/sdp"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"MediaMTX unavailable: {e}")
    ok = r.status_code < 300
    return Response(content=r.content, status_code=r.status_code,
                    media_type="application/sdp" if ok else "text/plain")
