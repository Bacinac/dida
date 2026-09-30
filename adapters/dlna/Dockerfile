# DLNA adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/dlna/ ./adapters/dlna/
RUN uv pip install --python /opt/venv/bin/python ./adapters/dlna

CMD ["python", "-m", "dida_adapter_dlna"]
