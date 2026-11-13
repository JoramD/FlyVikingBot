FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flyvikingbot ./flyvikingbot

# Runs as an unprivileged user (UID 1000); the host's ./data folder must be owned by UID 1000
RUN useradd --system --uid 1000 --user-group bot && mkdir -p /app/data && chown bot:bot /app/data
USER bot

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEBHOOK_PORT\",\"8080\")}/health',timeout=4)"

CMD ["python", "-m", "flyvikingbot"]
