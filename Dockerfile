# Multi-stage production Dockerfile for Sharad's Trading Desks
# Targets:
#   - runtime (default): minimal image for strangle-desk
#   - etf: adds pandas, numpy, and yfinance for etf-swing-desk
#   - directional-orb: pandas + numpy runtime for directional-orb-desk

FROM python:3.12-slim-bookworm AS builder-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./


FROM builder-base AS builder-runtime
RUN uv sync --frozen --no-dev --no-install-project --no-cache


FROM builder-base AS builder-etf
RUN uv sync --frozen --no-dev --no-install-project --no-cache --extra etf


FROM python:3.12-slim-bookworm AS runtime-base

ENV TZ=Asia/Kolkata \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata ca-certificates && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && \
    dpkg-reconfigure --frontend noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY config.py main.py run_strategy.py ./
COPY kite/ ./kite/
COPY notifications/ ./notifications/
COPY strategy/ ./strategy/
COPY scripts/run_weekly_etf_desk.py scripts/run_directional_orb_desk.py ./scripts/

RUN mkdir -p /app/data /app/logs

ENTRYPOINT ["python", "run_strategy.py"]
CMD ["--paper", "--lots", "1", "--poll", "5"]


FROM runtime-base AS etf
COPY --from=builder-etf /app/.venv /app/.venv


FROM runtime-base AS runtime
COPY --from=builder-runtime /app/.venv /app/.venv


FROM runtime-base AS directional-orb
COPY --from=builder-etf /app/.venv /app/.venv
ENTRYPOINT ["python", "scripts/run_directional_orb_desk.py"]
CMD ["--paper", "--lots", "5", "--orb-end", "09:45", "--otm-offset", "100", "--leg-sl-pct", "0.50", "--min-adx", "20", "--max-orb-width-pct", "0.65", "--exit-time", "15:15"]
