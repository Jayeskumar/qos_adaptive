#!/usr/bin/env bash
# Start FlowPilot locally (API + dashboard) on http://localhost:8000.
#   ./scripts/run_demo.sh            # SQLite, no setup
#   DATABASE_URL=postgresql+psycopg://flowpilot:flowpilot@localhost:5432/flowpilot ./scripts/run_demo.sh
# Kernel (real Linux testbed) mode needs root:  sudo -E ./scripts/run_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d frontend/dist ]; then
  echo "building dashboard..."
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi
cd backend
python3 -m pip install -q -r requirements.txt
export OMP_NUM_THREADS=1
exec python3 -m flowpilot.api
