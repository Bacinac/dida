# Harmony adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/harmony/ ./adapters/harmony/
RUN uv pip install --python /opt/venv/bin/python ./adapters/harmony

CMD ["python", "-m", "dida_adapter_harmony"]
