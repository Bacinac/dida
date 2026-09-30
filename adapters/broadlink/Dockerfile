# Broadlink adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/broadlink/ ./adapters/broadlink/
RUN uv pip install --python /opt/venv/bin/python ./adapters/broadlink

CMD ["python", "-m", "dida_adapter_broadlink"]
