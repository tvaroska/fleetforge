# =============================================================================
# One image, two commands.
# =============================================================================
# `api` (uvicorn) and `ingestor` (python -m) are the same image with a different
# `command:` — design/production.md -> "Open decisions": *One image, two commands*.
# Do not add a second Python Dockerfile; the CI build and the digest pin stay
# single because the image does.
#
# The project has no [build-system] in pyproject.toml, so it is never installed
# as a package: `uv sync --no-install-project` for the dependencies, then the
# source is put on PYTHONPATH. That mirrors pytest's `pythonpath = ["src", "."]`.
# =============================================================================

FROM ghcr.io/astral-sh/uv:0.9.1-python3.12-bookworm-slim AS base

WORKDIR /app

# Bytecode up front (slower build, faster cold start); copy rather than hardlink
# because the uv cache mount and /app are different filesystems.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

# curl is the healthcheck client for the api service in both stages.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# Development: dev dependencies, source arrives by bind mount from
# docker-compose.override.yml, command comes from compose (uvicorn --reload).
# ============================================
FROM base AS development

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "fleetforge.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ============================================
# Production: runtime dependencies only, source baked in, non-root.
# ============================================
FROM base AS production

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY src /app/src
# NOTHING FROM agent/dist IS COPIED HERE, and nothing should be (S0-infra-6). The
# prebuilt agent images are artifacts: `just agent-publish` uploads them to the object
# store and the flasher reads them back through the same seam R1's user artifacts use,
# so a firmware fix no longer needs an app-image rebuild and a redeploy. `.dockerignore`
# excludes `agent/` outright, which also keeps a firmware edit from invalidating this
# image's layers. See design/decisions/infrastructure-agent-bundles-are-artifacts.md.
# Alembic ships in the image: the api container runs `alembic upgrade head` from
# its entrypoint when RUN_MIGRATIONS=true (design/production.md -> Components).
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Non-root, and deliberately NOT the owner of /app: the code and the virtualenv
# are root-owned and world-readable, so a compromised api process cannot rewrite
# its own source. Nothing needs to write inside the image
# (PYTHONDONTWRITEBYTECODE=1 keeps the interpreter from trying).
RUN groupadd -r appuser && useradd -r -g appuser appuser
USER appuser

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "fleetforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
