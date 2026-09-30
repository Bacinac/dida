# Contacts adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/contacts/ ./adapters/contacts/
RUN uv pip install --python /opt/venv/bin/python ./adapters/contacts

CMD ["python", "-m", "dida_adapter_contacts"]
