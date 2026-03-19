# Stage 1: Build
FROM python:3.13.0-bullseye AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Stage 2: Production
FROM python:3.13.0-bullseye AS production

WORKDIR /app

RUN pip install uvicorn

COPY --from=build /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=build /app /app

EXPOSE 80
EXPOSE 443

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80", "--workers", "4"]
