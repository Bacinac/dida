# API: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

# pg_dump / pg_restore for the backup+restore endpoints. The base is Debian trixie
# (ships PG-client 17) but the server is Postgres 18, and pg_dump refuses a server
# newer than itself — so pull client 18 from PGDG. Only the client tools, no server.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-18 \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
USER dida

WORKDIR /app
COPY services/api/ ./services/api/
RUN uv pip install --python /opt/venv/bin/python ./services/api
# The /help articles: explain_dida answers from the same ones (dida_api.help_docs).
COPY ui/src/lib/help/ ./help/

CMD ["python", "-m", "dida_api"]
