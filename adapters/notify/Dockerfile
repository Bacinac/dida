# Notify adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/notify/ ./adapters/notify/
RUN uv pip install --python /opt/venv/bin/python ./adapters/notify

CMD ["python", "-m", "dida_adapter_notify"]
