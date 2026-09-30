# Shared base for all DIDA services. DIDA is I/O-bound (home automation is
# event-driven, not compute-heavy) so there is ONE base image — plain CPU
# Python. No GPU/variant matrix like BABA: vision/inference is delegated to
# BABA over the same NATS bus, so the home core needs no accelerator.
FROM python:3.14-slim AS base

# Stated here and nowhere else: services are FROM this image, so they inherit the
# rewritten sources and their own apt-get lines need no repetition.
COPY docker/apt-sources.sh /usr/local/bin/apt-sources
RUN apt-sources

# uv for fast, reproducible installs (same toolchain as BABA). Pinned so a
# rebuild never silently pulls a new uv; bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    # uv's default per-request timeout is 30 s, which is a bandwidth assumption in
    # disguise: a 3.3 MiB wheel needs ~35 s at the 0.79 Mbit/s the remote
    # installation actually has. Measured, not supposed — the Cabin upgrade on
    # 2026-08-08 died on exactly that ("Failed to download asyncpg==0.31.0" after
    # 108 s across uv's own retries). Every service and adapter image is FROM this
    # one, so setting it here covers the whole build.
    UV_HTTP_TIMEOUT=300

RUN uv venv /opt/venv

WORKDIR /app

# Reproducible installs (this is what makes the comment above true): the locked,
# pinned versions — docker/constraints.txt, exported from uv.lock — are applied to
# EVERY `uv pip install` in this image AND every image built FROM it, via the
# UV_CONSTRAINT env var. So `./core` here and each service's thin delta downstream
# resolve to the exact same locked set; a rebuild never silently bumps a version.
# Regenerate both after editing any pyproject dependency: docker/lock.sh.
COPY docker/constraints.txt ./constraints.txt
ENV UV_CONSTRAINT=/app/constraints.txt

# Core is shared by every service; install it into the base venv so each
# service image is a thin delta on top.
COPY core/ ./core/
RUN uv pip install --python /opt/venv/bin/python ./core

# dida-core exists here and nowhere else, yet every image built FROM this one asked
# the package index for it: a public name anyone could register, and a pypi 503 on
# it failed the Cabin deploy on 2026-09-25. Resolved from this directory instead.
RUN echo "dida-core @ file:///app/core" > ./overrides.txt
ENV UV_OVERRIDE=/app/overrides.txt

# Numbered SQL migrations — the single source of truth for the Postgres schema.
# apply_migrations() (dida_core.migrations) reads them from here at service boot.
COPY db/migrations/ ./db/migrations/

# ClickHouse (history firehose) migrations — apply_ch_migrations() reads them
# from here at engine boot (rollup tables + MVs; retention TTLs are data-driven).
COPY ch/migrations/ ./ch/migrations/

# Drop root. Everything above ran as root (installs); the venv + app tree are handed
# to `dida` so a service can still write its own bytecode cache, and every image
# built FROM this one starts non-root by default. The few services that genuinely
# need root — netmgr (macvlan/dhclient), runner (docker socket), govee (restarts its
# bridge) — switch back with an explicit `USER root` in their own Dockerfile; a build
# step that needs apt does the same around the apt block, then returns to `dida`.
RUN groupadd -g 1000 dida \
    && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin dida \
    && chown -R dida:dida /opt/venv /app
USER dida
