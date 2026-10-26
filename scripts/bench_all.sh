#!/usr/bin/env bash
# Reproduce every number in docs/REPORT.md.
set -euo pipefail
cd "$(dirname "$0")/../backend"
export OMP_NUM_THREADS=1
python3 -m flowpilot.ml.dataset --episodes 400 --out data/flows_sim.npz          # ~10 min on 4 cores
python3 -m flowpilot.ml.train_classifier --data data/flows_sim.npz               # ~1 min
python3 -m flowpilot.ml.rl.train --steps 60000 --envs 32                         # ~6 min
python3 -m flowpilot.bench.run_bench --seeds 1 2 3 --out results/sim_bench.json  # ~10 min on 4 cores
if [ "$(id -u)" = 0 ]; then
  python3 -m flowpilot.bench.kernel_bench --out results/kernel_bench.json        # ~45 min, real Linux
fi
python3 -m flowpilot.bench.plots --sim results/sim_bench.json --kernel results/kernel_bench.json
python3 -m flowpilot.db.importer results/sim_bench.json results/kernel_bench.json || true
