"""Загрузка, проверка и безопасная запись config.yaml."""
import os
import shutil

import yaml

# пароль API задаётся при запуске: python3 run.py -p <пароль>; переменная окружения - для служебного запуска
API_PASSWORD_ENV = "FACE_API_PASSWORD"


def apply_api_password(cfg):
    """Подставляет пароль API (из запуска run.py -p / FACE_API_PASSWORD) в cfg.connection.password.
    Возвращает cfg (тот же словарь); без пароля конфиг остаётся как есть."""
    pwd = os.environ.get(API_PASSWORD_ENV)
    if pwd and isinstance(cfg, dict):
        conn = cfg.setdefault("connection", {})
        if isinstance(conn, dict):
            conn["password"] = pwd
    return cfg


def parse_config(raw):
    """bytes/str -> dict. Бросает ValueError, если YAML невалиден или в нём нет обязательных настроек."""
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"некорректный YAML: {e}")
    return validate_config(apply_api_password(cfg))


def load_config(path):
    with open(path, "rb") as f:
        return parse_config(f.read())


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("конфиг должен быть YAML-словарём")

    missing = [k for k in ("mode", "camera", "turnstiles", "images_folder") if k not in cfg]
    if missing:
        raise ValueError("не хватает разделов: " + ", ".join(missing))

    mode = cfg["mode"]
    if not isinstance(cfg.get(mode), dict):
        raise ValueError(f"для mode: {mode} нужен одноимённый раздел '{mode}:' (embedding_folder, threshold, ...)")
    for key in ("embedding_folder", "threshold"):
        if key not in cfg[mode]:
            raise ValueError(f"в разделе '{mode}:' нет параметра {key}")

    source = cfg.get("source", "db")
    if source not in ("db", "excel"):
        raise ValueError("source должен быть 'db' или 'excel'")
    if source == "excel" and not cfg.get("excel_file"):
        raise ValueError("при source: excel нужен excel_file")
    if source == "db" and not isinstance(cfg.get("connection"), dict):
        raise ValueError("при source: db нужен раздел connection (host, login, password)")

    if not isinstance(cfg["camera"], dict) or cfg["camera"].get("id") in (None, ""):
        raise ValueError("в разделе camera нужен параметр id (RTSP-адрес или номер камеры)")

    for key in ("area_1", "area_2", "id_tur"):
        if key not in cfg["turnstiles"]:
            raise ValueError(f"в разделе turnstiles нет параметра {key}")
    for key in ("area_1", "area_2"):
        a = cfg["turnstiles"][key]
        if not (isinstance(a, (list, tuple)) and len(a) == 4 and all(isinstance(v, (int, float)) for v in a)):
            raise ValueError(f"turnstiles.{key} должен быть списком из 4 чисел [x, y, w, h]")

    fid = cfg["turnstiles"].get("face_pass_field_id", -4)
    if not isinstance(fid, int) or isinstance(fid, bool):
        raise ValueError("turnstiles.face_pass_field_id должен быть целым числом (id поля карточки Perco)")

    rtsp = cfg.get("rtsp_server") or {}
    if rtsp.get("enabled") and not rtsp.get("url"):
        raise ValueError("rtsp_server.enabled: true требует rtsp_server.url")
    return cfg


def write_config_atomic(path, raw):
    """Сохраняет новый конфиг, предварительно делая копию старого (config.yaml.bak)."""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    os.replace(tmp, path)
