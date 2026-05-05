# Trading bot container — runs Flask + APScheduler in one process.
# Persistent state lives in /app/data (mount as volume so it survives container restarts).
#
# Build:   docker build -t tradebox .
# Run:     docker compose up -d   (uses docker-compose.yml)

FROM python:3.11-slim

# System deps:
# - tzdata: scheduler needs Asia/Kuala_Lumpur timezone
# - curl: useful for healthchecks / debugging inside container
# - build-essential: needed for some Python packages on ARM (Synology/QNAP ARM models)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kuala_Lumpur \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=7550 \
    STOCK_RUN_SCHEDULER=1

WORKDIR /app

# Copy requirements first to leverage Docker layer cache (deps change rarely)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the app
COPY . .

# Persistent data lives in /app/data (volume-mounted).
# On first start it'll be empty; the app creates app.db automatically.
RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data", "/app/logs"]

EXPOSE 7550

# Healthcheck: hit the dashboard every 30s — restart if it 5xx's repeatedly
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:7550/tradar/ || exit 1

CMD ["python", "run.py"]
