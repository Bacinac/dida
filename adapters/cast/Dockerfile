# Cast adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/cast/ ./adapters/cast/
RUN uv pip install --python /opt/venv/bin/python ./adapters/cast

CMD ["python", "-m", "dida_adapter_cast"]
