# ---- Stockfish: built from source (default) or the distro package for quick local builds ----
FROM debian:bookworm-slim AS stockfish
ARG STOCKFISH_SOURCE=source
ARG SF_REF=sf_19
ARG SF_ARCH=x86-64-avx2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl make g++ stockfish \
    && rm -rf /var/lib/apt/lists/*
# If the tag or the compile fails, fall back to the distro binary rather than failing the deploy.
RUN if [ "$STOCKFISH_SOURCE" = "source" ] && \
       curl -fsSL "https://github.com/official-stockfish/Stockfish/archive/refs/tags/${SF_REF}.tar.gz" | tar xz -C /tmp && \
       cd /tmp/Stockfish-*/src && make -j"$(nproc)" build ARCH="${SF_ARCH}" && ./stockfish quit </dev/null; then \
      cp stockfish /usr/local/bin/stockfish; \
    else echo "using distro stockfish" && cp /usr/games/stockfish /usr/local/bin/stockfish; fi \
    && /usr/local/bin/stockfish quit </dev/null

# ---- API ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 COGNITO_STOCKFISH_PATH=/usr/local/bin/stockfish
COPY --from=stockfish /usr/local/bin/stockfish /usr/local/bin/stockfish
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
HEALTHCHECK CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8000\")}/api/health')" || exit 1
# Hosts such as Render inject $PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
