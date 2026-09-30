# Govee adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/govee/ ./adapters/govee/
RUN uv pip install --python /opt/venv/bin/python ./adapters/govee

CMD ["python", "-m", "dida_adapter_govee"]
