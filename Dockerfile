# Single-image deploy (used by render.yaml): React UI + FastAPI + Stockfish on one origin.
# docker-compose keeps using backend/Dockerfile + frontend/Dockerfile.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM debian:bookworm-slim AS stockfish
ARG STOCKFISH_SOURCE=source
ARG SF_REF=sf_19
# Portable build: the host CPU is not known at build time on managed platforms.
ARG SF_ARCH=x86-64-sse41-popcnt
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl make g++ stockfish \
    && rm -rf /var/lib/apt/lists/*
RUN if [ "$STOCKFISH_SOURCE" = "source" ] && \
       curl -fsSL "https://github.com/official-stockfish/Stockfish/archive/refs/tags/${SF_REF}.tar.gz" | tar xz -C /tmp && \
       cd /tmp/Stockfish-*/src && make -j"$(nproc)" build ARCH="${SF_ARCH}" && ./stockfish quit </dev/null; then \
      cp stockfish /usr/local/bin/stockfish; \
    else echo "using distro stockfish" && cp /usr/games/stockfish /usr/local/bin/stockfish; fi \
    && /usr/local/bin/stockfish quit </dev/null

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    COGNITO_STOCKFISH_PATH=/usr/local/bin/stockfish COGNITO_STATIC_DIR=/app/static
COPY --from=stockfish /usr/local/bin/stockfish /usr/local/bin/stockfish
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist ./static
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
