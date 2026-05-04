FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim as python_builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

FROM node:24-slim as node_builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim
WORKDIR /app

COPY --from=node_builder /usr/local/bin/node /usr/local/bin/node
COPY --from=node_builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node_builder /app/node_modules /app/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=python_builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

COPY . .

CMD ["uv", "run", "tools/validate.py"]
