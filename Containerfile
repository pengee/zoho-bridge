FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV BRIDGE_DB_PATH=/data/bridge.db \
    BRIDGE_SMTP_HOST=0.0.0.0

EXPOSE 1143 10587 8000
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "zoho_bridge"]
CMD ["--host", "0.0.0.0", "--port", "1143", "zoho"]
