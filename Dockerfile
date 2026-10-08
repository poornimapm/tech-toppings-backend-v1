# syntax=docker/dockerfile:1
# Production image for Render (free web service) and local production-parity runs.

ARG PYTHON_VERSION=3.11

# ---- build stage: resolve pinned dependencies into an isolated virtualenv ----------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY requirements/base.lock /tmp/base.lock
RUN pip install --requirement /tmp/base.lock

# ---- runtime stage: slim, non-root, no build tooling --------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    APP_ENV=production \
    LOG_FORMAT=json \
    HOST=0.0.0.0 \
    PORT=8000
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY --chown=app:app app ./app
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# Shell form so ${HOST}/${PORT} expand (Render injects PORT). FORWARDED_ALLOW_IPS is read by
# uvicorn itself. No --reload, no access log (the app logs requests itself, as JSON).
CMD exec uvicorn app.main:create_app --factory --host "${HOST}" --port "${PORT}" --proxy-headers --no-access-log
