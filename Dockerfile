# Stage 1: builder — uv resolves and materializes the locked venv from pyproject.toml + uv.lock.
# Two-pass sync optimizes layer caching: deps install first (rebuilds only when lockfile changes),
# then the project itself installs (rebuilds when src/ changes).
FROM python:3.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev

# Stage 2: runtime — slim base, no compiler. curl is included so the compose
# healthcheck `curl -fs http://localhost:8000/api/health` works inside the
# container. App user is UID 1000 to match `user: "1000:1000"` in compose.yml
# and to keep bind-mounted data under a non-root owner on Synology/Unraid.
FROM python:3.13-slim-trixie AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app . /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn finance_bro.main:app --host 0.0.0.0 --port 8000 --workers 1"]
