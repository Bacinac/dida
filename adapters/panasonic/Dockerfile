# Panasonic adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/panasonic/ ./adapters/panasonic/
RUN uv pip install --python /opt/venv/bin/python ./adapters/panasonic

CMD ["python", "-m", "dida_adapter_panasonic"]
