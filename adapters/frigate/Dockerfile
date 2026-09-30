FROM dida/base:latest AS base
WORKDIR /app
COPY adapters/frigate/ ./adapters/frigate/
RUN uv pip install --python /opt/venv/bin/python ./adapters/frigate
CMD ["python", "-m", "dida_adapter_frigate"]
