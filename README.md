# FlowPilot — an adaptive, learning QoS engine for mixed home broadband

> **Problem statement (Tata Elxsi TELIPORT S4):** *Adaptive QoS engine for mixed home
> broadband traffic.* Build a software-defined controller that learns which applications
> need bandwidth, and prove — with measured latency, loss and fairness — that it beats
> standard scheduling.

FlowPilot sits on the home gateway. It watches traffic **without reading payloads**,
recognises each flow's application from its behaviour (gaming, video call, 4K streaming,
web, bulk download, cloud backup), learns what each one needs, tracks the ISP link's
*real* capacity from latency probes, and reprograms the Linux kernel's traffic-control
tree every 500 ms so that interactive traffic never waits behind bulk traffic — while
downloads still use all the bandwidth nobody else needs.

**Read the full report: [`docs/REPORT.md`](docs/REPORT.md)** — research, design, algorithms,
evaluation methodology, every result, the demo script and the deployment path.

## What is in the box

| Layer | What we built |
|---|---|
| **Data plane** (real Linux) | HTB tree per direction, per-class time-bounded queues (or fq_codel), nftables + conntrack-mark classification, DSCP marking for Wi-Fi WMM / NQB. Needs only HTB — works on embedded CPE kernels that lack CAKE. |
| **Telemetry** | AF_PACKET flow monitor (headers only), `tc` class counters, 20 Hz reflector probes with kernel timestamps (one-way delay both directions). |
| **Learning** | Gradient-boosted flow classifier on 26 behavioural features (99.7 % accuracy on unseen households; 93 % of real-kernel flows and 99.62 % of their bytes, never having seen real traffic); online per-class demand learning; per-device profiles in PostgreSQL; residual-DQN autorate agent with a safety shield. |
| **Control** | Network-utility-maximisation allocator (inelastic first, then weighted proportional fairness), delay-based capacity tracking, RFC 9956-style traffic protection, household intents (gaming / work-from-home / movie night), plain-language decision log. |
| **Evaluation** | Packet-level digital twin (CUBIC TCP, FQ-CoDel/CAKE/HTB models) **and** a real-kernel Linux testbed (network namespaces, real TCP/UDP, TUN delay line), the same 6 scenarios on both, 8 router policies, QoE models (E-model MOS, gaming, P.1203-style video, G.1030 web), Jain + QoE fairness. |
| **Product** | FastAPI (REST + WebSocket), PostgreSQL (SQLite fallback), React dashboard with a live A/B mode: *same household, same congestion — standard router vs FlowPilot*. |

## Headline results

Measured on the **real Linux kernel testbed** — network namespaces, real kernel TCP/UDP,
real qdiscs — in the *evening rush* (game + Teams call + 4K Netflix + browsing while a Steam
update opens 8 TCP connections on a 50/10 Mb/s plan with a 250 ms ISP buffer):

| | Standard router | **FlowPilot** |
|---|---|---|
| Game round-trip time p50 / p99 | 240 / 277 ms | **28 / 45 ms** |
| Teams call one-way delay p99 | 271 ms | **40 ms** |
| Call video frames arriving too late | 25.7 % | **0.1 %** |
| Call voice quality (E-model MOS, 1-5) | 2.93 | **4.31** |
| Web page load (median) | 2.52 s | **1.10 s** |
| Netflix bitrate | 7.6 Mb/s | **14.1 Mb/s** (4K) |
| Game and call seconds within SLO | 6 % | **99 %** |
| Download throughput (lower effort, by design) | 42.4 Mb/s | 23.5 Mb/s |

<sub>Real Linux kernel, mean of 2 runs. FlowPilot identified 93 % of all flows (99.62 % of bytes) across its 24 kernel runs, from packet timing and sizes only.</sub>

Across all six scenarios of the digital twin (8 routers × 3 seeds), against the standard router:

| | Standard router | FlowPilot |
|---|---|---|
| Game RTT p99 | 279 – 937 ms | **40 – 100 ms** (−83 to −92 %) |
| Call one-way delay p99 | 272 – 921 ms | **33 – 86 ms** (−85 to −94 %) |
| Game and call seconds within SLO | 5 – 17 % | **85 – 100 %** |
| Interactive QoE, MOS 1-5 | 1.53 – 2.49 | **4.01 – 4.78** |

Against fq_codel, CAKE and CAKE + autorate FlowPilot is equivalent on a stable link, an order
of magnitude better than any fixed-rate shaper when the ISP's capacity changes, and the only
router that keeps a call clean when the same laptop is also syncing (call p99 33 vs 125-134 ms,
0 % vs 1.8-2.2 % packet loss). The cost is paid by bulk transfers (typically ~20 % slower
while interactive traffic is active). See [`docs/REPORT.md`](docs/REPORT.md) §7 for every
scenario, packet loss, fairness, the real-kernel results and the costs.

## Quick start

**Simulator mode (any OS, no root):**

```bash
cd frontend && npm ci && npm run build && cd ..        # dashboard
cd backend && pip install -r requirements.txt
python -m flowpilot.api                                # http://localhost:8000  (SQLite)
```

**With PostgreSQL (Docker):**

```bash
docker compose up --build                              # http://localhost:8000
```

**Real-kernel testbed (Linux, root):**

```bash
sudo ./scripts/setup_testbed.sh                        # installs iproute2/nftables/conntrack, checks the kernel
sudo -E ./scripts/run_demo.sh                          # the dashboard now offers "Linux testbed" mode
cd backend && sudo python3 -m flowpilot.bench.kernel_runner --scenario evening_rush --scheduler flowpilot
```

Then open the dashboard, keep **Compare with: Standard router (FIFO)**, press **Start session**,
and click **Steam update** / **ISP congestion −55%**. The 5-minute demo script is in the report (§8).

## Repository layout

```
backend/flowpilot/
  common/      app types & traffic classes, behavioural flow features, QoE metrics
  sim/         packet-level discrete-event simulator, TCP CUBIC, app models, qdisc models
  testbed/     netns topology, TUN delay line, tc/nftables data plane, AF_PACKET monitor,
               live traffic generators, kernel controller
  controller/  FlowPilot brain: classifier, allocator (NUM), autorate, brain, sim adapter
  ml/          dataset generation, classifier training, RL (fluid model, residual DQN, shield)
  bench/       scenarios, simulator & kernel benchmark runners, figures, report tables
  db/          SQLAlchemy models, async writer, results importer
  api/         FastAPI app, live sessions (sim worker / kernel), presets
  models/      trained classifier (joblib) and RL policy (npz)
backend/tests/ pytest suite (qdisc semantics, allocator invariants, QoE formulas, API, testbed)
frontend/      React + TypeScript + Recharts dashboard
docs/          REPORT.md and figures
scripts/       run_demo.sh, setup_testbed.sh, bench_all.sh (reproduces every number)
```

## Reproduce everything

```bash
cd backend && ../scripts/bench_all.sh      # dataset -> classifier -> RL -> benchmarks -> figures
python -m pytest -q                        # 15 tests
```

## License

No license file has been added yet, so the code is not yet licensed for reuse; add one (e.g. MIT)
before publishing. Third-party standards and papers are cited in the report.
