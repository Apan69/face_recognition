"""
Базовый класс движка распознавания.

Чтобы добавить новый движок, нужно:
  1. создать src/engines/<имя>.py с классом-наследником FaceEngine и реализовать ТРИ метода:
       load_model()          - загрузка модели
       encode_image(image)   - эмбеддинг самого крупного лица на фото (BGR) или None
       detect_faces(frame)   - лица на кадре: (список id пользователей, массив [top,left,bottom,right])
  2. добавить одну строку в словарь _ENGINES в src/engines/__init__.py
  3. добавить в config.yaml раздел с именем движка (embedding_folder, threshold, ...) и mode: <имя>

Хранение эмбеддингов, кодирование папки с фото, добавление нового фото, сопоставление - уже реализованы здесь.
Сопоставление по умолчанию - косинусное сходство (больше = похожее, совпадение при >= threshold).
Если у движка другая метрика (например, расстояние), переопределите match().
"""
import glob
import logging
import os
import pickle
import shutil
import sys
import time
from abc import ABC, abstractmethod

import cv2
import numpy as np

log = logging.getLogger("face_access.engine")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _unit(vec):
    vec = np.asarray(vec, dtype=np.float32).reshape(-1)
    return vec / (np.linalg.norm(vec) + 1e-9)


class FaceEngine(ABC):
    name = "base"

    def __init__(self, config, users):
        self.config = config
        self.cfg = config[config["mode"]]
        self.images_folder = config["images_folder"]
        self.embedding_folder = self.cfg["embedding_folder"]
        self.threshold = float(self.cfg["threshold"])
        self._db = ([], np.zeros((0, 0), np.float32))  # (список id, матрица эмбеддингов)
        self.load_model()
        self.load_embeddings(users)

    # ---------------------------------------------------------------- реализует движок
    @abstractmethod
    def load_model(self):
        ...

    @abstractmethod
    def encode_image(self, image_bgr):
        """Эмбеддинг (1-D float32) самого крупного лица на фото или None, если лица нет."""

    @abstractmethod
    def detect_faces(self, frame_bgr):
        """-> (user_ids: list[int], locations: ndarray Nx4 [top, left, bottom, right]); id 0 = не опознан."""

    # ---------------------------------------------------------------- общее
    def match(self, embedding):
        ids, matrix = self._db
        if not ids:
            return 0
        sims = matrix @ _unit(embedding)
        best = int(np.argmax(sims))
        return ids[best] if sims[best] >= self.threshold else 0

    @property
    def embeddings_count(self):
        return len(self._db[0])

    def _embedding_file(self, uid):
        return os.path.join(self.embedding_folder, str(uid))

    def has_embedding(self, uid):
        return os.path.exists(self._embedding_file(uid))

    def load_embeddings(self, users, quiet=False):
        """quiet=True: не перечислять сотрудников без эмбеддинга (при действиях со страницы, чтобы не засорять лог)."""
        ids, vecs = [], []
        for uid, name in users.items():
            if uid == 0:
                continue
            path = self._embedding_file(uid)
            if not os.path.exists(path):
                if not quiet:
                    log.warning("No embedding for id %s (%s), skipped", uid, name)
                continue
            try:
                with open(path, "rb") as f:
                    vecs.append(_unit(pickle.load(f)))
                ids.append(uid)
            except Exception as e:
                log.error("Cannot read embedding for id %s (%s): %s", uid, name, e)
        dim = vecs[0].shape[0] if vecs else 0
        keep = [i for i, v in enumerate(vecs) if v.shape[0] == dim]
        if len(keep) != len(vecs):
            log.error("Embeddings with different size skipped (были получены другим движком?)")
        matrix = np.stack([vecs[i] for i in keep]) if keep else np.zeros((0, 0), np.float32)
        self._db = ([ids[i] for i in keep], matrix)  # одно присваивание - безопасно для потока распознавания
        log.info("Embeddings loaded: %d of %d users", len(keep), len(users) - (1 if 0 in users else 0))

    def _save_embedding(self, uid, emb):
        os.makedirs(self.embedding_folder, exist_ok=True)
        with open(self._embedding_file(uid), "wb") as f:
            pickle.dump(_unit(emb), f)

    def encode_folder(self):
        """Считает эмбеддинги для новых/обновлённых фото (имя файла = id пользователя)."""
        os.makedirs(self.embedding_folder, exist_ok=True)
        stats = {"encoded": 0, "skipped": 0, "failed": 0}
        files = [f for f in sorted(glob.glob(os.path.join(glob.escape(self.images_folder), "*")))
                 if os.path.isfile(f) and os.path.splitext(f)[1].lower() in IMAGE_EXT]
        iterator = files
        if sys.stderr.isatty():
            try:
                import tqdm
                iterator = tqdm.tqdm(files)
            except ImportError:
                pass
        for file in iterator:
            uid = os.path.basename(file).split(".")[0]
            enc_file = os.path.join(self.embedding_folder, uid)
            if os.path.exists(enc_file) and os.path.getmtime(enc_file) >= os.path.getmtime(file):
                stats["skipped"] += 1
                continue
            img = cv2.imread(file)
            if img is None:
                log.warning("Cannot read image: %s", file)
                stats["failed"] += 1
                continue
            emb = self.encode_image(img)
            if emb is None:
                log.warning("No face found in: %s", file)
                stats["failed"] += 1
                continue
            with open(enc_file, "wb") as f:
                pickle.dump(_unit(emb), f)
            stats["encoded"] += 1
        log.info("Encode folder: %s", stats)
        return stats

    def _archive_photos(self, uid):
        """Фото сотрудника (<id>.*) -> images/_archive/<id>_<время>.<расширение>. Возвращает число перенесённых."""
        archive = os.path.join(self.images_folder, "_archive")
        moved = 0
        for old in glob.glob(os.path.join(glob.escape(self.images_folder), f"{uid}.*")):
            if os.path.isfile(old):
                os.makedirs(archive, exist_ok=True)
                stem, ext = os.path.splitext(os.path.basename(old))
                shutil.move(old, os.path.join(archive, f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"))
                moved += 1
        return moved

    def remove(self, uid, users):
        """«Убрать проход по лицу»: фото и эмбеддинг уходят в архив (images/_archive, <эмбеддинги>/_archive),
        сотрудник исчезает из базы распознавания. Возвращает True, если было что убирать."""
        removed = self._archive_photos(uid) > 0  # фото обязательно: иначе синхронизация пересоздаст эмбеддинг
        path = self._embedding_file(uid)
        if os.path.exists(path):
            archive = os.path.join(self.embedding_folder, "_archive")
            os.makedirs(archive, exist_ok=True)
            shutil.move(path, os.path.join(archive, f"{uid}_{time.strftime('%Y%m%d_%H%M%S')}"))
            removed = True
        self.load_embeddings(users, quiet=True)
        log.info("Face pass removed for id %s (%s): archived=%s", uid, users.get(uid), removed)
        return removed

    def enroll(self, uid, image_bgr, users, name=None):
        """Новое фото сотрудника: проверка лица -> сохранение фото -> эмбеддинг -> перезагрузка базы."""
        emb = self.encode_image(image_bgr)
        if emb is None:
            raise ValueError("На фото не найдено лицо")
        os.makedirs(self.images_folder, exist_ok=True)
        self._archive_photos(uid)  # старые фото этого сотрудника - в архив, чтобы не было двойников
        path = os.path.join(self.images_folder, f"{uid}.jpg")
        if not cv2.imwrite(path, image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise OSError(f"Не удалось сохранить {path}")
        self._save_embedding(uid, emb)
        self.load_embeddings(users, quiet=True)
        log.info("Photo enrolled for id %s (%s)", uid, name or users.get(uid))
        return path

    def sync(self, users):
        stats = self.encode_folder()
        self.load_embeddings(users)
        return stats

    def close(self):
        pass
