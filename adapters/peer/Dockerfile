# DIDA peer adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/peer/ ./adapters/peer/
RUN uv pip install --python /opt/venv/bin/python ./adapters/peer

CMD ["python", "-m", "dida_adapter_peer"]
