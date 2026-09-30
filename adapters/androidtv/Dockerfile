# Android TV adapter: thin layer on dida/base (which already has dida-core).
FROM dida/base:latest AS base

WORKDIR /app
COPY adapters/androidtv/ ./adapters/androidtv/
RUN uv pip install --python /opt/venv/bin/python ./adapters/androidtv

# /snap must exist in the image and belong to uid 1000: Docker seeds a fresh named
# volume from the image directory's ownership, and an absent path is created root-owned
# — which the non-root adapter then can't write.
USER root
RUN mkdir -p /snap && chown dida:dida /snap
USER dida

CMD ["python", "-m", "dida_adapter_androidtv"]
