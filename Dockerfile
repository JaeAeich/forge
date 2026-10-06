# One image, two roles: `forge api` and `forge worker`. Workers start sandboxes through a Docker socket
# (docker-compose) or as Sandbox resources (Kubernetes, see deploy/operator).
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && useradd --uid 10001 --create-home forge
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
COPY --from=web /web/dist ./web/dist
RUN uv sync --frozen --no-dev
ENV FORGE_DATA=/data PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["forge", "api", "--host", "0.0.0.0", "--port", "8000"]
