# Tuya adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/tuya/ ./adapters/tuya/
RUN uv pip install --python /opt/venv/bin/python ./adapters/tuya

CMD ["python", "-m", "dida_adapter_tuya"]
