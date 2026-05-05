FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS python_builder

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /opt

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

FROM node:lts-trixie-slim AS node_builder

WORKDIR /opt

COPY package.json package-lock.json ./
RUN npm ci

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -m appuser

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH="/opt/venv/bin:$PATH" \
    NODE_PATH="/opt/node_modules"

WORKDIR /app

COPY --from=node_builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node_builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node_builder /opt/node_modules /opt/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=python_builder --chown=appuser:appgroup /opt/venv /opt/venv
COPY --chown=appuser:appgroup . .

USER appuser

CMD ["uv", "run", "tools/validate.py"]
