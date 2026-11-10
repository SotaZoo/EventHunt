FROM node:22-slim AS web-assets
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/discovery ./discovery
COPY web/account ./account
COPY web/service ./service
COPY web/build-discovery.mjs ./
RUN npm run build:discovery

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY api ./api
COPY --from=web-assets /web/dist/discovery ./web/dist/discovery
COPY db/migrate ./db/migrate
COPY deploy ./deploy

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8080

CMD ["sh", "-c", "uv run nyc-ai-scout migrate && exec uv run nyc-ai-scout serve --host 0.0.0.0 --port ${PORT:-8080}"]
