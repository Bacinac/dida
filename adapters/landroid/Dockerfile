# Landroid adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/landroid/ ./adapters/landroid/
RUN uv pip install --python /opt/venv/bin/python ./adapters/landroid

CMD ["python", "-m", "dida_adapter_landroid"]
