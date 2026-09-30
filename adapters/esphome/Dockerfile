# ESPHome adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/esphome/ ./adapters/esphome/
RUN uv pip install --python /opt/venv/bin/python ./adapters/esphome

CMD ["python", "-m", "dida_adapter_esphome"]
