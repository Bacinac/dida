# Roidmi adapter: thin layer on dida/base (which already has dida-core + cryptography).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/roidmi/ ./adapters/roidmi/
RUN uv pip install --python /opt/venv/bin/python ./adapters/roidmi

CMD ["python", "-m", "dida_adapter_roidmi"]
