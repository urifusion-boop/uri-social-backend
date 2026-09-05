# Stage 1: Build
FROM python:3.13.0-bullseye AS build
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
FROM python:3.13.0-bullseye AS production
WORKDIR /app
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