# Samsung TV adapter: thin layer on dida/base (samsungtvws for the TV's local API).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/samsungtv/ ./adapters/samsungtv/
RUN uv pip install --python /opt/venv/bin/python ./adapters/samsungtv

CMD ["python", "-m", "dida_adapter_samsungtv"]
