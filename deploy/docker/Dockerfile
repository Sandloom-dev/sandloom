# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.4
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /source
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY packages ./packages
COPY migrations ./migrations
COPY tools/packaging ./tools/packaging
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
FROM ${PYTHON_IMAGE} AS runtime
# Runtime installs nothing; omit pip and its separate bundled CA store.
RUN python -m pip uninstall -y pip setuptools wheel \
    && useradd --create-home --uid 10001 appuser \
    && install -d -o appuser -g appuser /app/data
COPY --from=dependencies /opt/venv /opt/venv
COPY --chown=appuser:appuser migrations /app/migrations
ENV PATH="/opt/venv/bin:${PATH}" VIRTUAL_ENV=/opt/venv PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER appuser
EXPOSE 2222
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:2222/health/simple')" || exit 1
FROM runtime AS production
CMD ["gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", "sandloom.main:app"]
FROM runtime AS debug
CMD ["uvicorn", "sandloom.main:app", "--host", "0.0.0.0", "--port", "2222"]
