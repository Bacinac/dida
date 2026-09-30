# Ingress adapter: dida/base (dida-core) and nothing else. It only reads and writes
# services.conf — validating and applying the ingress happens on the Proxmox host,
# which is the only place that can reach Caddy, the connectors and the router.
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/cloudflare/ ./adapters/cloudflare/
RUN uv pip install --python /opt/venv/bin/python ./adapters/cloudflare

CMD ["python", "-m", "dida_adapter_cloudflare"]
