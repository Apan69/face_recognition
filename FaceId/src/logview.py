"""Чтение log.txt для страницы: записи за последние N часов (с учётом ротации log.txt.1, log.txt.2 ...)."""
import os
import re
from datetime import datetime, timedelta

STAMP = re.compile(r"^(\d\d/\d\d/\d\d \d\d:\d\d:\d\d) (\w+) ")     # формат из setup_logger: 02/11/26 20:38:15 WARNING ...
BAD_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
MAX_LINES = 3000


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return None


def read_log(path, hours, errors_only=False, max_lines=MAX_LINES):
    """-> (строки, обрезано ли). Строки без метки времени (трейсбеки) принадлежат предыдущей записи."""
    cutoff = datetime.now() - timedelta(hours=hours)
    chunks = []                                    # от новых файлов к старым
    for i in range(0, 8):
        lines = _read(path if i == 0 else f"{path}.{i}")
        if lines is None:
            if i == 0:
                return None, False
            break
        chunks.append(lines)
        first = next((STAMP.match(x) for x in lines if STAMP.match(x)), None)
        if first and datetime.strptime(first.group(1), "%d/%m/%y %H:%M:%S") <= cutoff:
            break                                  # граница периода уже внутри этого файла
    out = []
    for lines in reversed(chunks):                 # от старых к новым
        keep = False
        for line in lines:
            m = STAMP.match(line)
            if m:
                try:
                    keep = datetime.strptime(m.group(1), "%d/%m/%y %H:%M:%S") >= cutoff
                except ValueError:
                    keep = False
                if keep and errors_only:
                    keep = m.group(2) in BAD_LEVELS
            if keep:
                out.append(line)
    truncated = len(out) > max_lines
    return out[-max_lines:], truncated
