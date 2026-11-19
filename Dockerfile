FROM python:3.12-slim

# Shown on the ghcr.io package page; "source" links the image to the GitHub repo.
LABEL org.opencontainers.image.title="tailnetDash" \
      org.opencontainers.image.description="Self-hosted dashboard for a Tailscale network: devices, uptime, latency, timeline" \
      org.opencontainers.image.source="https://github.com/larryni/ts-dash" \
      org.opencontainers.image.url="https://github.com/larryni/ts-dash" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/dashboard.db \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py LICENSE ./
COPY static ./static

# Run as an unprivileged user; /data holds the SQLite database (mount a volume here).
RUN useradd --system --uid 10001 dashboard \
 && mkdir /data && chown dashboard /data
USER dashboard
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/auth/state', timeout=4)"

# One process only: the background poller lives inside it.
CMD ["python", "app.py"]
