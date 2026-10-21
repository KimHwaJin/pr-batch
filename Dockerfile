FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Cache third-party dependencies independently from application source changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
# config.yml (shared) and config.{APP_ENV}.yml (per environment); no
# ConfigMaps or Secrets. config.example.yml keeps the glob non-empty.
COPY logger.yml config*.yml ./
# Token verification public key(s) named by AUTH_PUBLIC_KEY_FILE.
COPY keys ./keys
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
# UID 1000 matches the Executor image so both can read the shared PV files.
RUN uv sync --locked --no-dev \
    && useradd --create-home --uid 1000 batch \
    && chown -R batch:batch /app

USER batch

EXPOSE 8000

# Listens on HOST/PORT from settings (config.yml, default 0.0.0.0:8000).
CMD ["pr-batch"]
