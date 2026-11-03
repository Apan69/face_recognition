"""ArcFace (InsightFace): детектор SCRFD + распознавание ArcFace, на GPU через onnxruntime."""
import logging
import threading
import warnings

import numpy as np

from .base import FaceEngine

log = logging.getLogger("face_access.arcface")

# Модель одна на процесс: при перезагрузке конфига она не грузится заново (экономия секунд и видеопамяти)
_APP_CACHE = {}
_APP_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()  # распознавание кадров и обработка фото из веб-интерфейса не пересекаются


def _load_app(cfg):
    import onnxruntime as ort
    from insightface.app import FaceAnalysis

    # CUDA/cuDNN из pip (onnxruntime-gpu[cuda,cudnn]) лежат в site-packages - подгружаем заранее
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as e:
            log.warning("onnxruntime.preload_dlls() failed: %s", e)

    use_cuda = "CUDAExecutionProvider" in ort.get_available_providers()
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if use_cuda else ["CPUExecutionProvider"]
    model_name = cfg.get("model_name", "buffalo_l")
    det_size = tuple(cfg.get("det_size", [640, 640]))
    det_thresh = float(cfg.get("det_thresh", 0.5))
    threads = int(cfg.get("cpu_threads", 2))

    key = (model_name, det_size, det_thresh, threads, use_cuda)
    with _APP_LOCK:
        if key in _APP_CACHE:
            return _APP_CACHE[key]

        # явное число потоков отключает привязку к ядрам (в контейнерах это сыпало ошибками affinity)
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        warnings.filterwarnings("ignore", category=FutureWarning, module="insightface")

        app = FaceAnalysis(name=model_name, allowed_modules=["detection", "recognition"],
                           providers=providers, sess_options=so)
        app.prepare(ctx_id=0 if use_cuda else -1, det_thresh=det_thresh, det_size=det_size)

        used = {t: m.session.get_providers()[0] for t, m in app.models.items()}
        log.info("ArcFace '%s' loaded, providers: %s", model_name, used)
        if use_cuda and any(p != "CUDAExecutionProvider" for p in used.values()):
            log.warning("GPU requested, but some models run on CPU - check CUDA/cuDNN (python check_gpu.py)")
        _APP_CACHE[key] = app
        return app


class ArcFaceEngine(FaceEngine):
    name = "arcface"

    def load_model(self):
        self.app = _load_app(self.cfg)

    def encode_image(self, image_bgr):
        with _INFER_LOCK:
            faces = self.app.get(image_bgr)
        if not faces:
            return None
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return np.asarray(face.normed_embedding, dtype=np.float32)

    def detect_faces(self, frame_bgr):
        with _INFER_LOCK:
            faces = self.app.get(frame_bgr)
        if not faces:
            return [], np.zeros((0, 4), np.float32)
        user_ids, locations = [], []
        for face in faces:
            x1, y1, x2, y2 = face.bbox
            locations.append([y1, x1, y2, x2])  # top, left, bottom, right
            user_ids.append(self.match(face.normed_embedding))
        return user_ids, np.asarray(locations, dtype=np.float32)
