# Announce adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/announce/ ./adapters/announce/
RUN uv pip install --python /opt/venv/bin/python ./adapters/announce

CMD ["python", "-m", "dida_adapter_announce"]
