# State engine: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY services/engine/ ./services/engine/
RUN uv pip install --python /opt/venv/bin/python ./services/engine

CMD ["python", "-m", "dida_engine"]
