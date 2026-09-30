# Runner: the only container allowed to drive compose, so an adapter can be
# switched on from the UI instead of by editing .env on the box.
FROM dida/base:latest AS base

# Stays root at runtime: drives compose over the Docker socket and rewrites the
# per-host .env. Never switches back to the base `dida` user.
USER root

# The standalone compose binary — no docker CLI needed, it speaks to the socket
# itself. Pinned: an unattended bump here would change how every service on the
# installation is (re)created.
ARG COMPOSE_VERSION=v2.40.3
ADD https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64 /usr/local/bin/docker-compose
RUN chmod 0755 /usr/local/bin/docker-compose

WORKDIR /app
COPY services/runner/ ./services/runner/
RUN uv pip install --python /opt/venv/bin/python ./services/runner

CMD ["python", "-m", "dida_runner"]
