# Event journal: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY services/journal/ ./services/journal/
RUN uv pip install --python /opt/venv/bin/python ./services/journal

CMD ["python", "-m", "dida_journal"]
