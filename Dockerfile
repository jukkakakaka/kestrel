# Kestrel — container image
# Local:  docker compose up
# Server: docker run -p 8000:8000 -v kestrel-data:/data kestrel

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    KESTREL_HOME=/data

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --no-dev --frozen --no-install-project

COPY src ./src
COPY assets ./assets

RUN uv sync --no-dev --frozen

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uv", "run", "kestrel", "serve", "--host", "0.0.0.0", "--port", "8000"]
