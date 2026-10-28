# Production image for helpdesk (mirrors the Sandloom framework image).
# ---------- Stage 1: dependency build ----------
FROM python:3.11-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY uv.loc[k] ./
# Install the LOCKED dependency set when uv.lock exists (reproducible builds);
# otherwise resolve from pyproject.toml.
RUN if [ -f uv.lock ]; then \
        uv export --frozen --no-dev --no-emit-project --output-file requirements.txt; \
    else \
        uv pip compile pyproject.toml --output-file requirements.txt; \
    fi \
    && uv pip install --system --no-deps -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS production
WORKDIR /app
ARG SITE=/usr/local/lib/python3.11/site-packages
COPY --from=builder ${SITE} ${SITE}
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . /app
ENV PYTHONUNBUFFERED=1 \
    GUNICORN_BIND=0.0.0.0:8000 \
    HEALTH_URL=http://localhost:8000/health/simple
# Install the project itself (deps already present), then drop root.
RUN python -m pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os, urllib.request as r; \
r.urlopen(os.environ['HEALTH_URL'])" || exit 1
# Worker count honours the container CPU quota (default min(2*cpus+1, 4),
# WEB_CONCURRENCY overrides) and only one worker runs the outbox relay.
CMD ["gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", "helpdesk.asgi:app"]
