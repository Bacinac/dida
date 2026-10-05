#!/usr/bin/env sh
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
docker run --rm -i --pull=never --memory=512m --memory-swap=512m \
  -v "$ROOT/services/planvision/src:/src:ro" -e PYTHONPATH=/src --entrypoint python dida/planvision:latest - <<'PY'
import gc
import resource
from io import BytesIO

import cv2
import numpy as np
from PIL import Image
from dida_planvision.pipeline import _load_gray, extract_borders, rooms_from_borders, erase_border_between

raw = np.zeros((2048, 4096, 4), dtype=np.uint16)
raw[400:1600, 600:3400] = [0, 0, 0, 65535]
ok, encoded = cv2.imencode('.png', raw)
assert ok
del raw
gc.collect()
gray, w, h = _load_gray(encoded.tobytes())
assert (w, h) == (1024, 512)
assert gray[0, 0] == 255 and gray[250, 500] == 0
extract_borders(gray)
frame = [[10, 10, 80, 1], [10, 89, 80, 1], [10, 10, 1, 80], [89, 10, 1, 80], [50, 10, 1, 80]]
rooms = rooms_from_borders(frame, 8192, 8192)
assert len(rooms) == 2
_, removed = erase_border_between(frame, rooms[0], rooms[1], 8192, 8192)
assert removed
stream = BytesIO()
Image.new('1', (8192, 8192)).save(stream, format='PNG')
try:
    _load_gray(stream.getvalue())
except ValueError as error:
    assert 'decoded pixels' in str(error)
else:
    raise AssertionError('Oversized decode was accepted')
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
assert peak < 400, f'Floor plan pipeline exceeded its memory budget: {peak:.1f} MiB'
print(f'PASS: 8192 canvas and maximum 16-bit RGBA decode in a 512 MiB container; peak RSS {peak:.1f} MiB')
PY
