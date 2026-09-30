# Presence adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/presence/ ./adapters/presence/
RUN uv pip install --python /opt/venv/bin/python ./adapters/presence

CMD ["python", "-m", "dida_adapter_presence"]
