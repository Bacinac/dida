# Denon/Marantz AVR adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/denon/ ./adapters/denon/
RUN uv pip install --python /opt/venv/bin/python ./adapters/denon

CMD ["python", "-m", "dida_adapter_denon"]
