# Диагностика журнала Perco.
#   python3 check_events.py configs/config.yaml            - логин и пароль берутся из connection в конфиге (рекомендуется)
#   python3 check_events.py configs/config.yaml 10         - вывести ВСЕ события контроллеров за последние 10 минут
#                                                            (нажмите кнопку вахтёра и запустите - покажет, какие события пишет Perco)
#   python3 check_events.py HOST LOGIN 'ПАРОЛЬ'            - вручную (пароль в одинарных кавычках)
# Проверяет запрос журнала с токеном в двух видах: как отдал Perco и с префиксом "Bearer ".
import sys

import requests
import yaml

minutes = None
if len(sys.argv) == 3 and sys.argv[2].isdigit():
    minutes = int(sys.argv[2])
    sys.argv = sys.argv[:2]
if len(sys.argv) == 2:
    with open(sys.argv[1], encoding="utf-8") as f:
        conn = (yaml.safe_load(f) or {}).get("connection") or {}
    import getpass
    import os
    host, login = conn.get("host"), conn.get("login")
    # пароль Perco в конфиге больше не хранится: переменная FACE_PERCO_PASSWORD или ввод с клавиатуры
    password = os.environ.get("FACE_PERCO_PASSWORD") or conn.get("password") or getpass.getpass(f"Пароль Perco для {login}: ")
elif len(sys.argv) == 4:
    host, login, password = sys.argv[1:4]
else:
    sys.exit(__doc__)

H = {"Content-type": "application/json; charset=UTF-8"}
r = requests.post(f"http://{host}/api/system/auth", json={"login": login, "password": password},
                  headers={**H, "Authorization": "Bearer null"}, timeout=10)
if r.status_code in (400, 401):
    sys.exit(f"Perco не принял вход: HTTP {r.status_code} (логин {login!r}, длина пароля {len(str(password))}). "
             "Проверьте логин и пароль.")
r.raise_for_status()
token = r.json()["token"]
print("вход выполнен; токен от Perco:", "с префиксом Bearer" if token.lower().startswith("bearer ") else "без префикса")
raw = token[7:] if token.lower().startswith("bearer ") else token
for name, value in (("как есть", token), ("с Bearer", "Bearer " + raw)):
    r = requests.get(f"http://{host}/api/eventsystem", headers={**H, "Authorization": value},
                     params={"beginDatetime": "2026-11-02 08:00", "endDatetime": "2026-11-02 09:00"}, timeout=20)
    print(f"{login}, токен {name}: HTTP {r.status_code}",
          f"строк: {len(r.json().get('rows', []))}" if r.ok else r.text[:200])

if minutes:
    from datetime import datetime, timedelta
    end = datetime.now()
    params = {"beginDatetime": (end - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M"),
              "endDatetime": (end + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")}
    r = requests.get(f"http://{host}/api/eventsystem", headers={**H, "Authorization": token}, params=params, timeout=30)
    r.raise_for_status()
    rows = sorted(r.json().get("rows", []), key=lambda x: x.get("id", 0))
    print(f"\nсобытия за {minutes} мин ({len(rows)}):")
    for x in rows:
        print(x.get("time_label"), "|", x.get("event_name_id"), x.get("event_name"), "|", x.get("res_name"),
              "№", x.get("resource_number"), "| из:", x.get("zone_exit"), "-> в:", x.get("zone_enter"), "|", x.get("fio") or x.get("user_name") or "")
