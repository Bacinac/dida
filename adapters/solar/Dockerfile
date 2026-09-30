# Solar-inverter adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/solar/ ./adapters/solar/
RUN uv pip install --python /opt/venv/bin/python ./adapters/solar

CMD ["python", "-m", "dida_adapter_solar"]
