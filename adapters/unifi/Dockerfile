# UniFi presence adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/unifi/ ./adapters/unifi/
RUN uv pip install --python /opt/venv/bin/python ./adapters/unifi

CMD ["python", "-m", "dida_adapter_unifi"]
