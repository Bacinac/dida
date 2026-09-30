# Calendar adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/calendar/ ./adapters/calendar/
RUN uv pip install --python /opt/venv/bin/python ./adapters/calendar

CMD ["python", "-m", "dida_adapter_calendar"]
