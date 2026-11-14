# ---- Stockfish: built from source (default) or the distro package for quick local builds ----
FROM debian:bookworm-slim AS stockfish
ARG STOCKFISH_SOURCE=source
ARG SF_REF=sf_19
ARG SF_ARCH=x86-64-avx2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl make g++ stockfish \
    && rm -rf /var/lib/apt/lists/*
RUN if [ "$STOCKFISH_SOURCE" = "source" ]; then \
      curl -fsSL "https://github.com/official-stockfish/Stockfish/archive/refs/tags/${SF_REF}.tar.gz" | tar xz -C /tmp \
      && cd /tmp/Stockfish-*/src && make -j"$(nproc)" build ARCH="${SF_ARCH}" \
      && cp stockfish /usr/local/bin/stockfish; \
    else cp /usr/games/stockfish /usr/local/bin/stockfish; fi \
    && /usr/local/bin/stockfish quit </dev/null

# ---- API ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 COGNITO_STOCKFISH_PATH=/usr/local/bin/stockfish
COPY --from=stockfish /usr/local/bin/stockfish /usr/local/bin/stockfish
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
