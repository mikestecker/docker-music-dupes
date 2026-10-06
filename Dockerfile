# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="music-dupes" \
      org.opencontainers.image.description="Find, review and quarantine duplicate music files" \
      org.opencontainers.image.source="https://github.com/mikestecker/docker-music-dupes" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    PORT=8095 \
    MUSIC_DIR=/music \
    CONFIG_DIR=/config

WORKDIR /app

# fpcalc (Chromaprint) for acoustic fingerprints. Optional at runtime: the app
# skips fingerprints when it's missing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/app.py app/index.html ./

# Default non-root user. Override with `user: "UID:GID"` in compose (or
# `--user` on docker run) so files you quarantine keep the right ownership.
# /config is world-writable so any UID you pick can write its state there, and
# the app files are made world-readable regardless of the host's umask.
RUN chmod -R a+rX /app && mkdir -p /config /music && chmod 0777 /config
USER 1000:1000

EXPOSE 8095
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8095\")}/healthz',timeout=4)" || exit 1

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port \"${PORT}\""]
