"""Запуск:  python3 run.py -p <пароль_API>   - веб-интерфейс + распознавание (остальные настройки: configs/config.yaml)
   Пароль API (connection.password) берётся из запуска, в config.yaml его хранить не нужно.
   Служебные ключи:
           python3 run.py -p <пароль> --encode   - только посчитать эмбеддинги для фото из images_folder
           python3 run.py -p <пароль> --list     - вывести список пользователей и сохранить Id_for_tele.xlsx
"""
import argparse
import os
import subprocess
import sys
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)  # относительные пути из конфига (images, frame, ...) считаются от папки проекта
CONFIG_FILE = os.environ.get("FACE_CONFIG", os.path.join(BASE_DIR, "configs", "config.yaml"))
os.environ["FACE_CONFIG"] = CONFIG_FILE


def ensure_cert(web, rtsp_url):
    """Самоподписанный сертификат (нужен, чтобы браузер разрешил доступ к веб-камере)."""
    cert = web.get("ssl_cert", "certs/cert.pem")
    key = web.get("ssl_key", "certs/key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    ips = {"127.0.0.1"} | {str(i) for i in web.get("cert_ips", [])}
    host = urlparse(rtsp_url or "").hostname
    if host:
        ips.add(host)
    san = ",".join([f"IP:{i}" for i in sorted(ips)] + ["DNS:localhost"])
    os.makedirs(os.path.dirname(cert) or ".", exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                    "-days", "825", "-subj", "/CN=face-access", "-addext", f"subjectAltName={san}"],
                   check=True, capture_output=True)
    print(f"Создан самоподписанный сертификат {cert} для: {san}")
    return cert, key


def cmd_encode(cfg):
    from src.engines import get_engine
    from src.utils import Connection, load_users, setup_logger
    setup_logger(cfg)
    users = load_users(cfg, Connection(cfg))
    print(get_engine(cfg, users).encode_folder())


def cmd_list(cfg):
    import pandas as pd
    from src.utils import Connection, load_users
    if cfg.get("source", "db") == "excel":
        print("Список из БД недоступен при source: excel")
        return
    users = load_users(cfg, Connection(cfg))
    users.pop(0, None)
    df = pd.DataFrame(sorted(users.items()), columns=["id", "name"])
    print(df.to_string())
    df.to_excel("Id_for_tele.xlsx", index=False)


def cmd_hash_password():
    """Пароль не показывается при вводе и не сохраняется; на выходе строка для config.yaml."""
    import getpass
    from src.auth import hash_password
    first = getpass.getpass("Пароль: ")
    if not first or first != getpass.getpass("Ещё раз: "):
        print("Пароли не совпали или пустые")
        return 1
    print("\nВместо строки password: ... в config.yaml впишите (с тем же отступом):\n")
    print(f"      password_hash: {hash_password(first)}")


def main():
    parser = argparse.ArgumentParser(prog="face-access")
    parser.add_argument("-p", "--password", metavar="ПАРОЛЬ_API", default=None,
                        help="пароль API системы допуска (connection.password); если не задан - берётся из config.yaml")
    parser.add_argument("-e", "--encode", action="store_true")
    parser.add_argument("-l", "--list", action="store_true")
    parser.add_argument("--hash-password", action="store_true", help="получить хеш пароля для auth.users[].password_hash")
    args = parser.parse_args()

    if args.hash_password:
        return cmd_hash_password()

    if args.password:
        from src.config import API_PASSWORD_ENV
        os.environ[API_PASSWORD_ENV] = args.password  # подставится в connection.password при загрузке конфига

    from src.config import load_config
    cfg = load_config(CONFIG_FILE)
    if args.encode:
        return cmd_encode(cfg)
    if args.list:
        return cmd_list(cfg)

    import uvicorn
    from main import app
    web = cfg.get("web") or {}
    kwargs = {"host": web.get("host", "0.0.0.0"), "port": int(web.get("port", 8000)), "log_level": "warning"}
    if web.get("ssl", True):
        try:
            kwargs["ssl_certfile"], kwargs["ssl_keyfile"] = ensure_cert(web, (cfg.get("rtsp_server") or {}).get("url"))
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"Не удалось создать сертификат ({e}), запуск по HTTP: камера в браузере работать не будет")
    scheme = "https" if "ssl_certfile" in kwargs else "http"
    print(f"Веб-интерфейс: {scheme}://<адрес-сервера>:{kwargs['port']}")
    uvicorn.run(app, **kwargs)  # один процесс: состояние сервиса общее


if __name__ == "__main__":
    sys.exit(main())
