FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo \
    PORT=3002 \
    DATA_DIR=/data \
    AUTO_REFRESH=true \
    REFRESH_ON_START=false \
    ENABLE_PLAYWRIGHT_FETCH=false \
    REFRESH_INTERVAL_HOURS=3

WORKDIR /app

# Install timezone data for accurate Asia/Tokyo (JST) date filtering
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY update_calendar.py split_casts.py server.py index.html admin.html calendar.ics /app/
COPY casts /app/casts
COPY tests /app/tests

RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 3002

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3002/healthz', timeout=4)" || exit 1

CMD ["python", "-u", "server.py"]
