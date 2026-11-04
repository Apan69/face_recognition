"""Загрузка, проверка и безопасная запись config.yaml."""
import os
import shutil
import sys

import yaml

# Пароль Perco не хранится в config.yaml: его передают при запуске сервиса
#   ExecStart=/usr/bin/python3 /faceId/run.py ПАРОЛЬ        (или переменной окружения FACE_PERCO_PASSWORD)
_runtime = {"password": None}


def set_runtime_password(password):
    _runtime["password"] = password or None


def runtime_password():
    if _runtime["password"]:
        return _runtime["password"]
    env = os.environ.get("FACE_PERCO_PASSWORD")
    if env:
        return env
    args = [a for a in sys.argv[1:] if not a.startswith("-")]      # run.py ПАРОЛЬ
    return args[0] if args else None


def apply_runtime_secrets(cfg):
    """Подставляет пароль Perco из аргумента запуска / окружения в connection.password (пароль из запуска главнее файла)."""
    pw = runtime_password()
    if pw and isinstance(cfg, dict) and isinstance(cfg.get("connection"), dict):
        cfg["connection"]["password"] = pw
    return cfg


def parse_config(raw):
    """bytes/str -> dict. Бросает ValueError, если YAML невалиден или в нём нет обязательных настроек."""
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"некорректный YAML: {e}")
    return validate_config(apply_runtime_secrets(cfg))


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
        raise ValueError("при source: db нужен раздел connection (host, login)")
    if source == "db" and not str(cfg["connection"].get("password") or ""):
        raise ValueError("не задан пароль Perco: передайте его при запуске (run.py ПАРОЛЬ) "
                         "или переменной окружения FACE_PERCO_PASSWORD")

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

    hours = cfg.get("log_view_hours", 6)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 720:
        raise ValueError("log_view_hours должен быть числом часов от 1 до 720 (сколько показывать на вкладке «Журнал»)")

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
