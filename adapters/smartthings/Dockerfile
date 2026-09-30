# SmartThings adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/smartthings/ ./adapters/smartthings/
RUN uv pip install --python /opt/venv/bin/python ./adapters/smartthings

CMD ["python", "-m", "dida_adapter_smartthings"]
