# Production Dockerfile for Sharad's Intraday Option Selling Desk
FROM python:3.12-slim-bookworm

# Configure Indian Standard Time (IST)
ENV TZ=Asia/Kolkata \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata ca-certificates && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && \
    dpkg-reconfigure --frontend noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install project dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source code
COPY config.py main.py run_strategy.py run_backtest.py ./
COPY kite/ ./kite/
COPY strategy/ ./strategy/
COPY notifications/ ./notifications/

# Ensure virtual environment binaries are in PATH
ENV PATH="/app/.venv/bin:$PATH"

# Create data directory for trade logs and state persistence
RUN mkdir -p /app/data

# Default entrypoint
ENTRYPOINT ["python", "run_strategy.py"]

# Default command (override in docker-compose.yml or docker run for live trading)
CMD ["--paper", "--lots", "1", "--poll", "5"]
