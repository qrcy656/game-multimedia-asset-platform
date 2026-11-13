FROM node:22-alpine AS narrative-build
WORKDIR /build
COPY narrative/package*.json ./
RUN npm ci --no-audit --no-fund
COPY narrative/index.html narrative/vite.config.js ./
COPY narrative/src ./src
COPY narrative/scripts ./scripts
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
COPY --from=narrative-build /build/static ./narrative/static
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
