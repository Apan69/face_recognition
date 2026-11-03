"""Реестр движков распознавания. Новый движок = новый файл + одна строка ниже (см. base.py)."""
import importlib

from .base import FaceEngine

# имя в config.yaml (mode:)  ->  "модуль:класс" (импортируется лениво, только выбранный движок)
_ENGINES = {
    "arcface": "src.engines.arcface:ArcFaceEngine",
}


def register_engine(name, target):
    """Для тестов и плагинов: target - класс или строка 'модуль:класс'."""
    _ENGINES[name] = target


def get_engine(config, users) -> FaceEngine:
    mode = config.get("mode")
    if mode not in _ENGINES:
        raise ValueError(f"Unsupported mode '{mode}'. Available: {', '.join(_ENGINES)}")
    target = _ENGINES[mode]
    if isinstance(target, str):
        module, cls = target.split(":")
        target = getattr(importlib.import_module(module), cls)
    return target(config, users)
