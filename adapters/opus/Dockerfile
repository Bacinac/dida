# OPUS adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/opus/ ./adapters/opus/
RUN uv pip install --python /opt/venv/bin/python ./adapters/opus

CMD ["python", "-m", "dida_adapter_opus"]
