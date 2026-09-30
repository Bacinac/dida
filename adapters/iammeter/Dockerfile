# IAMMETER grid-meter adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/iammeter/ ./adapters/iammeter/
RUN uv pip install --python /opt/venv/bin/python ./adapters/iammeter

CMD ["python", "-m", "dida_adapter_iammeter"]
