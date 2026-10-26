# FlowPilot dashboard

React 18 + TypeScript + Vite + Recharts front-end for the FlowPilot adaptive QoS engine.
It talks to the FastAPI service in `backend/flowpilot/api` over REST (`/api/*`) and a
live WebSocket (`/ws/live`, one telemetry tick per second).

| Tab | What it shows |
|---|---|
| **Live** | Start a session (simulator or Linux testbed), optionally as an A/B comparison (e.g. *Standard router (FIFO)* vs *FlowPilot*). One-click events (Steam update, photo backup, ISP congestion...), per-side topology, KPI tiles with red/amber/green QoE, latency / throughput / Netflix charts, the AI decision log, the classified flow table and per-class bandwidth allocation. |
| **Benchmarks** | Results of the benchmark matrix (all scenarios x routers), bar charts per metric, a sortable table and per-second series per scenario. |
| **AI models** | Classifier model card (accuracy on unseen households, confusion matrix, accuracy by flow age, feature importance) and the RL autorate evaluation. |
| **History** | Runs stored in the database (PostgreSQL or SQLite), their telemetry and decisions, and the device profiles FlowPilot has learned. |
| **How it works** | Architecture diagram and design notes. |

## Run

Production (single process): build once, then the API serves the app at `http://<host>:8000/`.

```bash
cd frontend
npm install
npm run build          # -> frontend/dist, served by FastAPI (/assets/* + SPA fallback)
cd ../backend && python -m flowpilot.api
```

Development with hot reload (Vite on :5173 proxies `/api` and `/ws` to the API):

```bash
cd backend && python -m uvicorn flowpilot.api.main:app --port 8001   # API
cd frontend && npm run dev                                           # http://localhost:5173
# point the proxy elsewhere with: FLOWPILOT_API=http://127.0.0.1:8000 npm run dev
```

Type-check only: `npm run typecheck`.

## Demo script (2 minutes)

1. **Live** tab -> Network *Simulation*, Router *FlowPilot (AI)*, Compare with *Standard router (FIFO)*, Scenario *Live household* -> **Start session**.
   A game, a Teams call, 4K Netflix and web browsing start on both identical households.
2. Click **Steam update**. Within seconds the standard router's game ping and call delay climb to ~250 ms
   (bufferbloat in the ISP's buffer) while FlowPilot holds them near the 30 ms base RTT; the hero strip shows the ratio.
3. Click **ISP congestion -55%**: FlowPilot's autorate detects the bufferbloat and lowers its shaper (see the
   decision log and the shaper line in the throughput chart). **Restore ISP** brings it back.
4. Try **OneDrive sync (laptop)** (uploads on the same laptop as the call) and change the **Intent** selector on the
   FlowPilot column (e.g. *Work from home*) to see the allocation re-weight.
5. Open **Benchmarks** for the measured comparison against FIFO, static QoS, fq_codel, CAKE and CAKE + autorate.

## Design notes

* Dark theme by default (projectors), light theme via the header toggle; both use their own validated colour steps.
* Colour roles: FlowPilot = blue, today's standard router = orange, other queue managers = grey; application types
  use fixed categorical slots; QoE status (good / fair / poor / bad) always pairs colour with an icon and a word.
* Charts use thin 2 px lines, solid hairline grids, legends for every multi-series chart and hover tooltips.
* No external fonts or CDNs: the dashboard works fully offline.
