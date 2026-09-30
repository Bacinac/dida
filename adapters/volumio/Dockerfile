# Volumio adapter: thin layer on dida/base (which already has dida-core + aiohttp).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/volumio/ ./adapters/volumio/
RUN uv pip install --python /opt/venv/bin/python ./adapters/volumio

CMD ["python", "-m", "dida_adapter_volumio"]
