FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CONFIG_DIR=/config PORT=8095 PUID=99 PGID=100
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

VOLUME ["/config"]
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8095\")}/api/health',timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
