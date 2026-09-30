# HEOS adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/heos/ ./adapters/heos/
RUN uv pip install --python /opt/venv/bin/python ./adapters/heos

CMD ["python", "-m", "dida_adapter_heos"]
