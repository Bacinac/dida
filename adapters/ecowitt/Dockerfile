# Ecowitt adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/ecowitt/ ./adapters/ecowitt/
RUN uv pip install --python /opt/venv/bin/python ./adapters/ecowitt

CMD ["python", "-m", "dida_adapter_ecowitt"]
