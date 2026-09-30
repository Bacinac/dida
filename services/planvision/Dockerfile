# planvision: raster floor plan → vector rooms (classical CV, CPU). Its OWN base,
# NOT dida/base, because it talks HTTP only and shares no code with the rest of DIDA.
FROM python:3.14-slim AS base

# opencv-python-headless still dlopens libGL-less GLib + OpenMP at runtime on slim.
COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources && apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the same uv as docker/base.Dockerfile — `uv:latest` here meant this one
# image silently re-resolved its toolchain on every rebuild.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/
# Cap decoded image size (64 MP): OpenCV's imdecode returns empty above this →
# _load_gray's `undecodable image` ValueError → 415, so a decompression-bomb PNG
# can't OOM the container.
ENV OPENCV_IO_MAX_IMAGE_PIXELS=67108864 \
    UV_LINK_MODE=copy \
    # Its own base inherits nothing from docker/base.Dockerfile — including the
    # download timeout a slow uplink needs. See the note there.
    UV_HTTP_TIMEOUT=300 \
    UV_COMPILE_BYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN uv venv /opt/venv

WORKDIR /app
# Same locked versions the rest of the stack builds against. Without this, opencv
# and numpy floated to whatever satisfied the pyproject bounds on each rebuild —
# so a silent minor bump could drift imdecode/morphology behaviour (and with it
# the extracted room outlines) behind a green build.
COPY docker/constraints.txt ./constraints.txt
ENV UV_CONSTRAINT=/app/constraints.txt
COPY services/planvision/ ./services/planvision/
RUN uv pip install --python /opt/venv/bin/python ./services/planvision

# Drop root (its own base doesn't inherit the shared one). Stateless HTTP CV — no
# /state, no privileged ops — so a plain non-root user is enough.
RUN groupadd -g 1000 dida \
    && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin dida \
    && chown -R dida:dida /opt/venv /app
USER dida
CMD ["python", "-m", "dida_planvision"]
