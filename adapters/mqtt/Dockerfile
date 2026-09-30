# MQTT adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/mqtt/ ./adapters/mqtt/
RUN uv pip install --python /opt/venv/bin/python ./adapters/mqtt

CMD ["python", "-m", "dida_adapter_mqtt"]
