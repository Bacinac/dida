# Astro adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/astro/ ./adapters/astro/
RUN uv pip install --python /opt/venv/bin/python ./adapters/astro

CMD ["python", "-m", "dida_adapter_astro"]
