# Runway API and worker image for the hosted profile (spec 05 section 7).
#
#   runway serve       the API (uvicorn, behind a TLS reverse proxy)
#   runway worker      the job worker (provided by the job-queue work package)
#   runway db upgrade  migrations; run as a release step with the owner role
#
# Built in CI on every push and never pushed anywhere from there. Secrets come
# from the host's secret store at run time; none are baked into the image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUNWAY_PROFILE=hosted \
    RUNWAY_HOST=0.0.0.0 \
    RUNWAY_API_PORT=8000 \
    RUNWAY_DATA_DIR=/data

# ffmpeg/ffprobe validate uploaded media (spec 02).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 runway \
    && useradd --system --uid 10001 --gid runway --home-dir /app --shell /usr/sbin/nologin runway

WORKDIR /app

# The exact locked environment first, so source edits do not reinstall it.
COPY requirements.lock ./
RUN python -m pip install --requirement requirements.lock

# Editable install, as in CI: Settings.project_root resolves alembic.ini and
# the migration scripts from the source tree.
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN python -m pip install --no-deps --editable . \
    && mkdir -p /data \
    && chown runway:runway /data

USER runway
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

ENTRYPOINT ["runway"]
CMD ["serve"]
