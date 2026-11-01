# OpsFleet data-analysis chat assistant (delaray/datachat).
# Secrets are never baked into this image; see docs/docker-image.md.

FROM ghcr.io/astral-sh/uv:0.8.22 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim
LABEL org.opencontainers.image.title="datachat" \
      org.opencontainers.image.description="OpsFleet data-analysis chat assistant (Gemini + BigQuery + PostgreSQL)" \
      org.opencontainers.image.version="1.0.0"
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATACHAT_GRANTS_FILE=/app/config/grants.executives.json
RUN useradd --create-home --uid 10001 datachat
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY main.py ./
COPY src ./src
COPY config/grants.executives.json config/grants.example.json ./config/
COPY knowledge ./knowledge
COPY db ./db
COPY docker/entrypoint.py /usr/local/bin/datachat-entrypoint.py
# /app/.local holds traces; mount a volume here (see compose.yaml).
RUN mkdir -p /app/.local && chown datachat:datachat /app/.local
VOLUME ["/app/.local"]
USER datachat
ENTRYPOINT ["python", "/usr/local/bin/datachat-entrypoint.py"]
CMD ["--help"]
