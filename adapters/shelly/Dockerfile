# Shelly adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/shelly/ ./adapters/shelly/
RUN uv pip install --python /opt/venv/bin/python ./adapters/shelly

CMD ["python", "-m", "dida_adapter_shelly"]
