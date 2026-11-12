# Stage 1: Build
# bookworm, not bullseye, since 2026-09-05: bullseye is EOL and its security binaries
# have been pulled from the pool while the index still advertises them, so apt resolves
# a version and then 404s fetching it. Verified: bullseye-security advertises
# libswscale5 7:4.3.9-0+deb11u2 and libssl1.1 1.1.1w-0+deb11u8, and the pool has ZERO
# .debs for either. Every build failed this way, on multiple CDN backends and re-runs.
# It is not fixable per-package — anything needing a security-updated dep hits it.
# NOTE: this moves ffmpeg from 4.3 (bullseye) to 5.1 (bookworm) in the production stage.
FROM python:3.13.0-bookworm AS build
# Force rebuild: Custom Visual Guides UriResponse fixes - 2026-06-11
ARG BUILD_DATE=2026-06-11
ENV BUILD_DATE=${BUILD_DATE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# libssl-dev was dropped 2026-09-05: it forced an upgrade of libssl1.1 to
# 1.1.1w-0+deb11u8, which bullseye-security's index advertises but no longer has in
# its pool — every build 404'd on that one .deb, across multiple CDN backends, so it
# was not transient and re-running never helped. Nothing in requirements.txt compiles
# against OpenSSL headers (the usual suspects — pycurl/M2Crypto/psycopg2/cryptography
# built from source — are all absent), and Python's own ssl module links the libssl1.1
# already in the base image, which is untouched. gcc stays for C extension builds.
# The retry config mirrors what the production stage below already sets.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Stage 2: Production
FROM python:3.13.0-bookworm AS production
WORKDIR /app
# Live-diagnosed 2026-10-01: memory utilization climbed to ~100% during AI
# video-generation testing (full video/image byte buffers moving through
# video_generation_service.py / video_storyboard_service.py — a Veo clip,
# say, fully in memory before it's re-uploaded to Cloudinary) and then NEVER
# came back down, even once CPU went idle for hours afterward — confirmed via
# the ECS service's own CPU/Memory utilization graphs. That shape (RSS stays
# pinned well after the triggering work stops, despite the Python objects
# being properly garbage collected) is glibc's per-thread malloc arena
# behavior on a bookworm/glibc base image, not a reference leak in app code:
# glibc keeps the memory arenas it allocated for large buffers rather than
# returning them to the OS, and a multi-worker/multi-threaded process like
# this (uvicorn --workers 4, each doing blocking SDK calls via
# run_in_executor's thread pool) can end up with many such arenas. This is
# exactly what was killing uvicorn worker processes via Linux's OOM killer —
# "Child process died" with no Python traceback, since a SIGKILL from the
# kernel bypasses application-level exception handling entirely.
# MALLOC_ARENA_MAX=2 is the standard, well-documented fix for this class of
# symptom in containerized Python/glibc services — caps the number of arenas
# glibc will create instead of scaling them with thread/core count, which
# bounds how much memory can get stuck unreturned this way. Pure allocator
# tuning, no application behavior change.
ENV MALLOC_ARENA_MAX=2
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries && \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install uvicorn
COPY --from=build /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=build /app /app

RUN curl -fsSL -o /app/global-bundle.pem \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

EXPOSE 80
EXPOSE 443
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80", "--workers", "4"]