# Virtual adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/virtual/ ./adapters/virtual/
RUN uv pip install --python /opt/venv/bin/python ./adapters/virtual

CMD ["python", "-m", "dida_adapter_virtual"]
