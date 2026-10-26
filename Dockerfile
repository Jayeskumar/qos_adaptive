# FlowPilot: API + dashboard in one image.
#   docker compose up --build            -> http://localhost:8000 (simulator mode, PostgreSQL)
#   docker compose --profile testbed up  -> also enables the real-Linux testbed (privileged)

FROM node:20-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
# iproute2/nftables/conntrack/ethtool are only needed for the Linux testbed mode
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 nftables conntrack ethtool iputils-ping \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY docs/ docs/
COPY --from=web /app/frontend/dist frontend/dist
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["python", "-m", "flowpilot.api"]
