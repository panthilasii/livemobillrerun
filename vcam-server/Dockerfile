# NP Create license/admin server.
#
# Two-stage build keeps the runtime image small:
# 1. ``builder`` installs deps into a virtualenv (compiles bcrypt
#    once).
# 2. ``runtime`` copies the venv + app code, runs uvicorn.
#
# We base on ``python:3.12-slim`` (Debian) because the bcrypt wheel
# needs a glibc — Alpine's musl works but the compile step is slow
# enough that a 30 MB image-size delta isn't worth the friction.

# ── builder ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /venv \
    && /venv/bin/pip install -r requirements.txt

# ── runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/var/lib/npcreate \
    BIND_HOST=0.0.0.0 \
    BIND_PORT=8000 \
    COOKIE_SECURE=1

# Non-root runtime user. Distroless would be nicer but bcrypt's
# python wheel needs libc; the slim image gives us that for ~70 MB.
RUN useradd --create-home --uid 10001 npc \
    && mkdir -p /var/lib/npcreate \
    && chown -R npc:npc /var/lib/npcreate

WORKDIR /app
COPY --from=builder /venv /venv
COPY app ./app
RUN chown -R npc:npc /app

USER npc

EXPOSE 8000

# We don't use ENTRYPOINT because operators sometimes want to shell
# in and run ``app.cli`` commands; a CMD lets ``docker compose run
# app python -m app.cli init-keys`` work without --entrypoint
# overrides.
CMD ["/venv/bin/uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD /venv/bin/python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
