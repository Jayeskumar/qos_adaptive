## 7. Results

The problem statement asks for *measured latency, packet loss and fairness improvements
against standard scheduling*. §7.1-7.3 answer exactly that on the digital twin (all eight
routers), §7.4 repeats it on the real Linux kernel, and §7.5-7.9 explain the mechanisms,
the learning components and the costs.

### 7.1 Latency and experience (digital twin, 6 scenarios × 8 routers × 3 seeds = 144 runs)

Full per-scenario tables (all eight routers, seventeen metrics) are in Appendix A. The
condensed view compares the standard router, the best *fixed-rate* SQM for each metric
(static QoS, fq_codel or CAKE, named in brackets), the strongest open-source competitor
(CAKE + autorate) and FlowPilot:

{{HEADLINE_TABLE}}

**What this says.**

* **Versus standard scheduling (the problem statement's baseline)**, FlowPilot cuts the
  99th-percentile game RTT by **83-92 %** and the call's one-way delay by **85-94 %** in
  every scenario, raises the share of game and call seconds within their SLO from
  **5-17 % to 85-100 %**, and lifts interactive QoE from "poor" (MOS 1.5-2.5) to
  "good/excellent" (4.0-4.8).
* **Versus fixed-rate SQM** (fq_codel, CAKE), FlowPilot is equivalent on a stable link
  (within 5-7 ms at the 99th percentile) and **an order of magnitude better whenever the
  ISP's capacity changes** (ISP congestion: 71 vs 596 ms; 5G FWA: 100 vs 978 ms), because a
  fixed shaper pushes the queue back into the ISP's buffer as soon as the true capacity
  drops below it.
* **Versus CAKE + autorate** — the best open-source combination, which needs CAKE in the
  kernel — FlowPilot is within 6 ms when capacity moves and **wins decisively when
  contention is on the uplink or inside one device**: call p99 **37 vs 62 ms** in the
  upload storm and **33 vs 134 ms** when the laptop on the call is also syncing (SLO 100 %
  vs 75 %). No fairness-only scheduler can solve the second case: the call and the sync are
  the same host, and per-flow fairness gives the 3 Mb/s call only 1/5 of a 10 Mb/s uplink.
  FlowPilot knows which flow is the call.
* **Application awareness pays off for video too:** FlowPilot streams Netflix at 14.1 Mb/s
  (4K) while a Steam update runs, versus 5.7 (FIFO) and 7.2 (fq_codel); and, against CAKE +
  autorate, at +12 % under ISP congestion (11.5 vs 10.3 Mb/s), +19 % beside the
  16-connection download (13.8 vs 11.6) and +32 % under 5G-FWA fading (11.9 vs 9.0).

### 7.2 Packet loss — and the loss that real-time media actually suffers

Standard scheduling rarely *drops* game or call packets: the ISP's deep buffers absorb
them — and deliver them hundreds of milliseconds late. A video frame that arrives after the
receiver has had to play out is as lost as a dropped one, so we report both: network
packet loss, and the share of the call's video frames that arrived late or incomplete
(the picture freezes), plus the time Netflix spent rebuffering.

{{LOSS_TABLE}}

* **Standard router:** network loss is only 0.02-0.11 %, but **21-75 % of the call's video
  frames arrive too late** and Netflix rebuffers up to a third of the time (download hog).
* **AQM trades delay for drops.** When a call shares a queue with bulk traffic — fq_codel
  in the upload storm, WFH sync and download hog; CAKE and CAKE + autorate in WFH sync,
  where call and sync share a host — CoDel **drops 1.8-2.2 % of the call's packets** and
  11.6-14.1 % of its frames still arrive late.
* **FlowPilot: 0.00 % call packet loss in five scenarios and 0.01 % under FWA fading, no
  late frames except 1.4 % during deep FWA fades, and zero rebuffering everywhere.** The
  call never shares a queue with a bulk flow, so nothing has to be dropped to keep it fast.

### 7.3 Fairness

We measure two kinds of fairness:

1. **Throughput fairness between devices** — Jain's index [25] over the downloading
   devices. In the download hog a PC opens 16 connections against a tablet's one: per-flow
   schedulers hand the PC ~94 % of the link.
2. **QoE fairness** — Hoßfeld's $F = 1 - 2\sigma/(H-L)$ over the MOS of *every* application
   in the home [26], including bulk transfers (scored by their throughput relative to an
   equal share). $F = 1$ means everyone has the same experience.

{{FAIRNESS_TABLE}}

* **Jain:** 0.56-0.57 for FIFO, static QoS and fq_codel; **0.98 for FlowPilot** (and CAKE),
  thanks to one HTB leaf per device with a $BDP/\sqrt{n}$ buffer (§4.5).
* **QoE fairness:** the standard router is *unfair* (F = 0.23-0.35: some applications are
  fine, others unusable); FlowPilot reaches 0.71-0.85 and is the fairest router when the
  contention is inside one device (WFH sync: 0.85 vs ≤ 0.66 for every other router).
* **Honest caveat:** on variable links (ISP congestion, FWA), CAKE + autorate scores higher
  F (0.82-0.83 vs 0.71-0.76) because FlowPilot deliberately gives bulk transfers a
  lower-effort share (§7.9), and F counts a slower download the same as a frozen call. The
  household intent (§4.5) sets this trade-off; "balanced" favours interactive use.

### 7.4 Real Linux kernel testbed

{{KERNEL_SECTION}}

### 7.5 Scenario by scenario (digital twin)

**Evening rush (classic bufferbloat).** The Steam update starts at t = 20 s. Under the
standard router the ISP's 250 ms buffer fills within two seconds and stays full: every
packet of the game and the call waits behind it (Figure 4). FlowPilot classifies the eight
Steam connections as *Bulk* within one control period (sibling inheritance labels the
later ones instantly), gives the game and call guarantees sized on their burst peaks, and
lets the download borrow everything else (Figure 5): the game stays at its ~29 ms base
RTT plus a few ms. On this stable link CAKE is the best AQM and FlowPilot's portable
bfifo variant trails it by ~5 ms at the 99th percentile; with fq_codel leaves it matches
it (44 / 37 ms).

![Figure 4 - latency timeline](figures/latency_timeline.png)

*Figure 4 — evening rush: p95 latency per second. The standard router sits at 200-280 ms
for as long as the download runs; FlowPilot and CAKE stay near the base RTT.*

![Figure 5 - allocation](figures/allocation_stack.png)

*Figure 5 — downlink by application. FlowPilot lets the download fill every gap (pink)
while the call (orange) and Netflix's ON/OFF segment fetches (yellow) get what they need.*

**Upload storm.** A 10 Mb/s uplink with four photo-backup uploads. FIFO: 548 / 522 ms.
fq_codel keeps the game low (its flow is sparse) but not the call (139 ms), which is *not*
sparse: as one flow among five it is offered ~1.9 Mb/s of a 9.5 Mb/s uplink but needs 3.
CAKE's host isolation helps (68 ms) because the backup runs on a different device. FlowPilot
guarantees the call its measured peak: 37 ms, MOS 4.32, SLO 100 %. Pure TCP ACKs ride in the
Interactive class, so the download that starts at 30 s still gets its ACK clock (41.7 Mb/s).

**Work-from-home sync (same device).** The laptop on the Teams call starts a 4-connection
OneDrive sync. Every fairness-based scheduler fails the same way — fq_codel, CAKE and
CAKE + autorate all put the call at 125-134 ms p99, MOS ≈ 3.0, only 75-77 % of seconds
within SLO — because the call and the sync share a host. FlowPilot: **33 ms, MOS 4.34,
100 %**. The price: the sync uploads at 6.2 instead of 7.7-8.0 Mb/s.

**ISP congestion (capacity drop).** From 45 s to 85 s the access capacity falls from 50/10 to
22/6 Mb/s. Fixed-rate shapers keep shaping at 47.5 Mb/s, so the queue moves into the ISP:
~600 ms for 40 s (Figure 6). FlowPilot's autorate sees the one-way delay jump within
~150 ms, cuts the shaper to 92 % of the delivered rate, and re-probes upwards quickly once
the link recovers (the time-decayed bloat memory of §4.6). CAKE + autorate, using the same
delay logic, behaves similarly; FlowPilot keeps Netflix higher (11.5 vs 10.3 Mb/s).

![Figure 6 - congestion](figures/congestion_timeline.png)

*Figure 6 — top: true ISP capacity and each router's shaper; bottom: what the gamer feels.*

**Download hog (fairness).** A PC opens 16 connections, a tablet downloads over one.
Per-flow fairness (FIFO, fq_codel) hands the PC 94 % of the bandwidth (Jain 0.56-0.57).
CAKE and FlowPilot are both device-fair (Jain 0.98) — FlowPilot through one HTB leaf per
device and a $BDP/\sqrt{n}$ buffer that lets a single connection keep its share — but
FlowPilot additionally keeps the 4K stream at 13.8 Mb/s versus 11.6 Mb/s (Figure 7).

![Figure 7 - fairness](figures/fairness.png)

*Figure 7 — left: per-device download rates (seed 1); right: Netflix bitrate under the same load.*

**5G FWA fading.** Capacity wanders between 15 and 60 Mb/s with deep fades (Figure 8). Every
fixed shaper is wrong most of the time: ~1 s latency, 5-7 % of seconds within SLO. Only the
capacity-tracking routers stay usable (85-87 % of seconds within SLO); here the residual-RL
autorate gains 2.8 points of utilisation and slightly lower call delay over FlowPilot's
heuristic (§7.8).

![Figure 8 - FWA](figures/fwa_timeline.png)

*Figure 8 — 5G FWA: capacity vs shaper, and game latency.*

### 7.6 Latency matrix and QoE

![Figure 9 - p99 matrix](figures/p99_matrix_sim.png)

*Figure 9 — 99th-percentile game RTT and call delay, every scenario × router (mean of 3 seeds).*

![Figure 10 - QoE](figures/qoe_sim.png)

*Figure 10 — interactive QoE (bars: mean MOS of game, call, video and web; tick: the worst of
them). A FlowPilot variant has the best interactive MOS and the best worst-application MOS
in five of six scenarios; the exception is the stable-link evening rush, where CAKE leads by
0.04-0.07 MOS.*

### 7.7 Classification

The classifier card is in §4.4.4: 99.66 % accuracy / 0.993 macro-F1 on unseen households,
99.6 % within the first second of a flow. Live, inside the controller (classifier +
hysteresis + sibling inheritance + traffic protection), every flow that received a label
— including flows that ended long before the run did — was scored against the generator's
ground truth:

{{ACCURACY}}

### 7.8 Reinforcement-learning autorate

![Figure 11 - RL](figures/rl_autorate.png)

*Figure 11 — left: learning curve on 16 held-out links (thin lines: the heuristic on the same
links); right three panels: 64 held-out links by link type.*

On 64 held-out links of the training model, the residual DQN is **Pareto-better than the
hand-tuned heuristic** overall:

| | Utilisation | Delay p95 | Time with > 20 ms queueing | Mean reward |
|---|---:|---:|---:|---:|
| Static 95 % shaper | 99.8 % | 1 916 ms | 86.1 % | −8.41 |
| Heuristic autorate (cake-autorate style) | 73.1 % | 121 ms | 16.1 % | −0.203 |
| **Residual DQN + shield** | **75.4 %** | 127 ms | **12.7 %** | **−0.091** |

By link type: on fixed lines with congestion steps it halves the time spent above 20 ms
(11.1 → 6.3 %) and cuts the p95 from 45 to 27 ms at equal utilisation; on 5G FWA it gains
6 points of utilisation (68 → 74 %) with less time above 20 ms (20 → 18 %), at a slightly
higher p95 (227 → 241 ms, dominated by deep fades no controller can hide).

**Reading the learning curve.** We judge policies by the two operational criteria in the
figure — utilisation and the share of time with more than 20 ms of queueing — not by the
scalar reward, because the reward's delay penalty dominates: on the 16 learning-curve links
the untrained, always-conservative policy scores a *higher* reward (−0.005) than the final
policy (−0.076) while using only 48 % of the link. Training moves the policy from 48 % / 6 %
to 78 % / 13 %, against the heuristic's 73 % / 16 % on the same links (its reward there is
−0.214; the −0.203 in the table is on the 64 evaluation links). A constrained formulation
(maximise utilisation subject to a delay budget) is the natural next step.

**Transfer to the packet simulator** (same scenarios as §7.1, 3 seeds):

| Scenario | Utilisation heuristic → RL | Game p99 | Call p99 |
|---|---|---|---|
| 5G FWA fading | 78.6 → **81.4 %** | 100 → 103 ms | 86 → **85 ms** |
| ISP congestion | 86.2 → 86.1 % | 71 → **68 ms** | 63 → **61 ms** |
| Evening rush (stable) | 90.0 → 85.2 % | 48 → **46 ms** | 41 → **39 ms** |
| Upload storm (stable) | 89.4 → 86.0 % | 45 → **43 ms** | 37 → **37 ms** |

So the agent transfers where it was meant to help — variable links — and is a little more
conservative than needed on stable lines. FlowPilot therefore ships the heuristic as the
default and the RL policy as the recommended mode for FWA/LTE gateways (§10).

### 7.9 What it costs, and where FlowPilot does not win

* **Bulk throughput.** By design (RFC 8622 "lower effort"), downloads and uploads yield to
  everything else while interactive traffic is active. Against CAKE + autorate they get
  3-39 % less, about 20 % in a typical contended scenario: 24.0 vs 26.3 Mb/s in the evening
  rush, 15.3 vs 19.4 in ISP congestion, 25.5 vs 32.5 for the download hog, 6.2 vs 8.0 Mb/s
  uplink in WFH sync and 8.5 vs 14.0 under FWA fading — the bandwidth that kept Netflix in
  4K and the call clean. Selecting a different intent changes this trade-off.
* **Utilisation.** FlowPilot shapes at ≤ 95 % of the plan (the SQM margin) and cuts on
  bufferbloat, so it uses 2-3 points less of the link than CAKE + autorate (which may probe
  to 100 %).
* **Stable-link tail latency.** With plain bfifo leaves, FlowPilot's p99 is ~5 ms above
  CAKE's on a stable line (game 48 vs 43 ms); the fq_codel-leaf variant closes the gap where
  the kernel offers fq_codel.
* **FWA game latency.** Under deep fades every capacity-tracking router briefly reaches
  ~100 ms; FlowPilot is not better than CAKE + autorate there (100 vs 99 ms).
* **QoE fairness on variable links** is lower than CAKE + autorate's (§7.3), the mirror image
  of the bulk-throughput cost.
