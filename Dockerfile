# Stage 1: Build
# bookworm, not bullseye, since 2026-09: bullseye is EOL and its security binaries
# have been pulled from the pool while the index still advertises them, so apt
# resolves a version and then 404s fetching it — every build 404'd on libssl1.1
# 1.1.1w-0+deb11u8, not transient, re-running never helped. Brought over from
# aws/dev (commits 381fa62 / 53063cd) where this was already fixed; prod's build
# had been broken since ~2026-09-04 for this exact reason.
FROM python:3.13.0-bookworm AS build

# Force rebuild: Custom Visual Guides UriResponse fixes - 2026-06-11
ARG BUILD_DATE=2026-06-11
ENV BUILD_DATE=${BUILD_DATE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# libssl-dev dropped alongside the bookworm move: nothing in requirements.txt
# compiles against OpenSSL headers, and Python's ssl module links the base
# image's own libssl. gcc stays for other C extension builds.
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
