# Midea adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/midea/ ./adapters/midea/
RUN uv pip install --python /opt/venv/bin/python ./adapters/midea

CMD ["python", "-m", "dida_adapter_midea"]
