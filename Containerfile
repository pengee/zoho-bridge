FROM python:3.12-slim

WORKDIR /app

# non-root runtime user; the ports used (1143/10587/8000) are all >1024 so
# an unprivileged user can still bind them
RUN groupadd -g 10001 bridge && useradd -m -u 10001 -g bridge bridge

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV BRIDGE_DB_PATH=/data/bridge.db \
    BRIDGE_SMTP_HOST=0.0.0.0 \
    BRIDGE_HEALTH_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# /data holds the SQLite DB + generated TLS cert; must be writable by the
# runtime user. chown before VOLUME so a fresh named volume inherits it.
RUN mkdir -p /data && chown bridge:bridge /data

USER bridge

EXPOSE 1143 10587 8000
VOLUME ["/data"]

# healthz binds loopback *and* the container iface (BRIDGE_HEALTH_HOST=0.0.0.0
# here) so this in-container probe and host `-p 127.0.0.1:8000:8000` both work
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "zoho_bridge"]
CMD ["--host", "0.0.0.0", "--port", "1143", "zoho"]
