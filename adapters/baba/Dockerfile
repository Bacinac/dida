# BABA adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/baba/ ./adapters/baba/
RUN uv pip install --python /opt/venv/bin/python ./adapters/baba

CMD ["python", "-m", "dida_adapter_baba"]
