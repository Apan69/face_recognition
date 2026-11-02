"""
Проверка окружения для ArcFace (InsightFace):
  * версии numpy / onnxruntime / insightface / opencv
  * конфликт onnxruntime и onnxruntime-gpu
  * доступен ли CUDAExecutionProvider
  * реально ли модели InsightFace работают на GPU (а не молча упали на CPU)
  * сравнение скорости GPU и CPU

Запуск:  python.exe check_gpu.py
         python.exe check_gpu.py --image images\\125.jpg   (дополнительно проверит распознавание на фото)
"""
import argparse
import platform
import subprocess
import sys
import time
import traceback
from importlib import metadata

OK, BAD, WARN = '[ OK ]', '[FAIL]', '[WARN]'
problems = []


def section(title):
    print(f'\n=== {title} ===')


def fail(msg):
    problems.append(msg)
    print(f'{BAD} {msg}')


def pkg_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def nvidia_smi(query):
    try:
        out = subprocess.check_output(
            ['nvidia-smi', f'--query-gpu={query}', '--format=csv,noheader,nounits'],
            text=True, stderr=subprocess.STDOUT, timeout=15)
        return [line.strip() for line in out.strip().splitlines()]
    except Exception:
        return None


def gpu_mem_used():
    res = nvidia_smi('memory.used')
    try:
        return int(res[0]) if res else None
    except ValueError:
        return None


parser = argparse.ArgumentParser()
parser.add_argument('--image', help='путь к фото с лицом для проверки распознавания')
parser.add_argument('--model', default='buffalo_l')
parser.add_argument('--runs', type=int, default=30)
args = parser.parse_args()

# ---------------------------------------------------------------- 1. пакеты
section('1. Python и установленные пакеты')
print(f'Python      : {sys.version.split()[0]} ({platform.architecture()[0]}) on {platform.platform()}')
for name in ['numpy', 'onnxruntime', 'onnxruntime-gpu', 'insightface', 'onnx',
             'opencv-python', 'opencv-python-headless', 'scipy', 'scikit-image']:
    v = pkg_version(name)
    print(f'{name:24}: {v if v else "-"}')

if pkg_version('onnxruntime') and pkg_version('onnxruntime-gpu'):
    fail('Установлены ОДНОВРЕМЕННО onnxruntime и onnxruntime-gpu - они конфликтуют, '
         'CPU-версия может перекрывать GPU.\n'
         '       Исправление: pip uninstall -y onnxruntime onnxruntime-gpu && pip install onnxruntime-gpu')
elif not pkg_version('onnxruntime-gpu'):
    fail('Пакет onnxruntime-gpu не установлен (pip install onnxruntime-gpu)')
else:
    print(f'{OK} Только onnxruntime-gpu, конфликта нет')

if pkg_version('opencv-python') and pkg_version('opencv-python-headless'):
    print(f'{WARN} Установлены и opencv-python, и opencv-python-headless - лучше оставить один '
          '(для cv2.imshow нужен opencv-python)')

# ---------------------------------------------------------------- 2. импорты
section('2. Импорт библиотек (проверка совместимости с numpy)')
try:
    import numpy as np
    print(f'{OK} numpy {np.__version__}')
    import cv2
    print(f'{OK} opencv {cv2.__version__}')
    import onnxruntime as ort
    print(f'{OK} onnxruntime {ort.__version__}')
    if hasattr(ort, 'preload_dlls'):
        try:
            ort.preload_dlls()
            print(f'{OK} ort.preload_dlls() выполнен (CUDA/cuDNN из pip подгружены)')
        except Exception as e:
            print(f'{WARN} ort.preload_dlls() не сработал: {e}')
    import insightface
    from insightface.app import FaceAnalysis
    print(f'{OK} insightface {insightface.__version__}')
except Exception:
    traceback.print_exc()
    print('\nИмпорт упал. Частая причина - бинарная несовместимость с numpy 2.x:\n'
          '  pip install "numpy<2"   (или обновите onnxruntime-gpu / insightface / opencv до версий с поддержкой numpy 2)')
    sys.exit(1)

# ---------------------------------------------------------------- 3. провайдеры
section('3. Провайдеры onnxruntime')
providers = ort.get_available_providers()
print(f'ort.get_device()   : {ort.get_device()}')
print(f'Доступные провайдеры: {providers}')
if 'CUDAExecutionProvider' in providers:
    print(f'{OK} CUDAExecutionProvider доступен')
else:
    fail('CUDAExecutionProvider НЕ доступен - onnxruntime видит только CPU.\n'
         '       Проверьте: установлен onnxruntime-gpu (не onnxruntime), драйвер NVIDIA, а также версии CUDA и cuDNN\n'
         '       под вашу версию onnxruntime-gpu (таблица: onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)')
    print('\nДальнейшие тесты GPU бессмысленны.')
    sys.exit(1)

# ---------------------------------------------------------------- 4. nvidia-smi
section('4. Видеокарта (nvidia-smi)')
info = nvidia_smi('name,driver_version,memory.total,memory.used')
if info:
    print(f'{OK} {info[0]}  (имя, драйвер, MiB всего, MiB занято)')
else:
    print(f'{WARN} nvidia-smi недоступен или не отвечает')

# ---------------------------------------------------------------- 5. загрузка модели
section(f'5. Загрузка модели {args.model} на GPU')
DET_SIZE = (640, 640)


def build(prov, ctx_id, threads=2):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    app = FaceAnalysis(name=args.model, allowed_modules=['detection', 'recognition'],
                       providers=prov, sess_options=so)
    app.prepare(ctx_id=ctx_id, det_size=DET_SIZE)
    return app


mem_before = gpu_mem_used()
gpu_app = build(['CUDAExecutionProvider', 'CPUExecutionProvider'], 0)

all_on_gpu = True
for task, model in gpu_app.models.items():
    used = model.session.get_providers()
    on_gpu = bool(used) and used[0] == 'CUDAExecutionProvider'
    all_on_gpu &= on_gpu
    print(f'{OK if on_gpu else BAD} {task:12} -> {used}')
if not all_on_gpu:
    fail('Часть моделей работает на CPU. Смотрите сообщения выше вида "Failed to create CUDAExecutionProvider" / '
         '"cannot open shared object file" (Linux) / "LoadLibrary failed with error 126" (Windows) - это почти всегда '
         'отсутствующие библиотеки CUDA/cuDNN: libcudart, libcublas, libcudnn и т.д.\n'
         '       Решение: pip install "onnxruntime-gpu[cuda,cudnn]" в этом же окружении.')

mem_after = gpu_mem_used()
if mem_before is not None and mem_after is not None:
    delta = mem_after - mem_before
    print(f'Память GPU: было {mem_before} MiB, стало {mem_after} MiB (+{delta} MiB)')
    if delta > 50:
        print(f'{OK} Видеопамять реально заняна моделью')
    else:
        print(f'{WARN} Рост видеопамяти ничтожный - GPU, возможно, не используется')


# ---------------------------------------------------------------- 6. бенчмарк
def bench(app, runs):
    det, rec = app.models['detection'], app.models['recognition']
    frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
    face = np.random.randint(0, 255, (112, 112, 3), dtype=np.uint8)
    for _ in range(5):  # прогрев (на GPU первый вызов очень медленный)
        det.detect(frame)
        rec.get_feat(face)
    t = time.perf_counter()
    for _ in range(runs):
        det.detect(frame)
    det_ms = (time.perf_counter() - t) / runs * 1000
    t = time.perf_counter()
    for _ in range(runs):
        rec.get_feat(face)
    rec_ms = (time.perf_counter() - t) / runs * 1000
    return det_ms, rec_ms


section(f'6. Скорость ({args.runs} прогонов, кадр 1280x720)')
gpu_det, gpu_rec = bench(gpu_app, args.runs)
print(f'GPU: детекция {gpu_det:7.1f} мс | эмбеддинг лица {gpu_rec:6.1f} мс')

import os
cpu_app = build(['CPUExecutionProvider'], -1, threads=len(os.sched_getaffinity(0)))
cpu_det, cpu_rec = bench(cpu_app, max(5, args.runs // 3))
print(f'CPU: детекция {cpu_det:7.1f} мс | эмбеддинг лица {cpu_rec:6.1f} мс')

speedup = cpu_det / gpu_det if gpu_det else 0
print(f'Ускорение детекции GPU/CPU: x{speedup:.1f}')
if speedup >= 1.5:
    print(f'{OK} GPU заметно быстрее CPU - ускорение работает')
else:
    fail('GPU не быстрее CPU - скорее всего, инференс идёт на CPU или упирается в загрузку/накладные расходы')

# ---------------------------------------------------------------- 7. реальное фото
if args.image:
    section(f'7. Распознавание на фото {args.image}')
    img = cv2.imread(args.image)
    if img is None:
        fail(f'Не удалось прочитать {args.image}')
    else:
        faces = gpu_app.get(img)
        print(f'Найдено лиц: {len(faces)}')
        for i, f in enumerate(faces):
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            norm = float(np.linalg.norm(f.normed_embedding))
            print(f'  лицо {i}: bbox=({x1},{y1},{x2},{y2}) score={f.det_score:.2f} '
                  f'эмбеддинг={f.normed_embedding.shape} норма={norm:.3f}')
        if not faces:
            fail('На фото лицо не найдено')

# ---------------------------------------------------------------- итог
section('ИТОГ')
if problems:
    print(f'{BAD} Проблем: {len(problems)}')
    for p in problems:
        print(f'  - {p.splitlines()[0]}')
    sys.exit(1)
print(f'{OK} Всё в порядке: numpy/onnxruntime-gpu/insightface работают, модели выполняются на GPU')
