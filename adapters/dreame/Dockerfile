# Dreame adapter: thin layer on dida/base (stdlib only — the cloud client is urllib).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/dreame/ ./adapters/dreame/
RUN uv pip install --python /opt/venv/bin/python ./adapters/dreame

CMD ["python", "-m", "dida_adapter_dreame"]
