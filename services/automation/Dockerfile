# Automation engine: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY services/automation/ ./services/automation/
RUN uv pip install --python /opt/venv/bin/python ./services/automation

CMD ["python", "-m", "dida_automation"]
