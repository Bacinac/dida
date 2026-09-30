# Network manager: thin layer on dida/base (which already has dida-core).
# Adds iproute2 (VLAN sub-interfaces) + isc-dhcp-client (obtain a lease from the
# router). Runs privileged (NET_ADMIN, host netns) — see docker-compose.yml.
FROM dida/base:latest AS base

# Stays root at runtime: creates macvlan networks over the Docker socket and runs
# dhclient (raw sockets / NET_ADMIN). Never switches back to the base `dida` user.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 isc-dhcp-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/netmgr/ ./services/netmgr/
RUN uv pip install --python /opt/venv/bin/python ./services/netmgr

CMD ["python", "-m", "dida_netmgr"]
