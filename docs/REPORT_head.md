# FlowPilot — an Adaptive, Learning QoS Engine for Mixed Home-Broadband Traffic

*Tata Elxsi TELIPORT Season 4 · Problem statement: "Adaptive QoS engine for mixed home
broadband traffic"*

> **Build a software-defined network controller that learns which applications need
> bandwidth. Simulate a network with gaming, video calls, Netflix and large downloads;
> introduce congestion; show the AI controller automatically reducing latency for
> critical applications; demonstrate measured latency, packet loss and fairness
> improvements against standard scheduling.**

---

## 0. Executive summary

**What we built.** FlowPilot is a controller for the home gateway that

1. **learns which application every flow is** — game, video call, video stream, web,
   bulk download, cloud backup — from packet *timing and size patterns only* (no payload
   inspection, no port lists; works on encrypted traffic), with a gradient-boosted model
   that is **99.7 % accurate on households it has never seen** and that labelled
   **{{K_ACC_FLOWS}} of real Linux kernel flows ({{K_ACC_BYTES}} of bytes) correctly without ever
   being trained on real traffic**;
2. **learns how much each one needs** — burst peaks of calls, the bitrate a video player
   settles on, whether a download could use more — and remembers it per device in
   PostgreSQL;
3. **learns the link** — tracks the ISP's true, changing capacity from one-way-delay
   probes (a heuristic controller, plus a **residual deep-Q-network agent with a safety
   shield** that is Pareto-better on held-out links);
4. **allocates bandwidth as a utility-maximisation problem** — latency-critical traffic
   first (protected but capped), then weighted proportional fairness by household intent,
   per-device fairness for downloads, no class ever starved — and programs it into the
   **real Linux kernel** (HTB + nftables + conntrack marks) every 500 ms,
   explaining every decision in plain language.

**How we proved it.** Two independent testbeds running the *same* scenarios, the same
controller code and the same metric code: a packet-level **digital twin** (CUBIC TCP,
Linux-faithful FIFO / FQ-CoDel / CAKE / HTB models) for 8 routers × 6 scenarios × 3 seeds,
and a **real-kernel Linux testbed** (network namespaces, real kernel TCP/UDP, real qdiscs,
a userspace WAN delay line) for the 4 routers its kernel supports × 6 scenarios × 2 runs.
Baselines: a standard router, classic port-based QoS, fq_codel, CAKE and CAKE + autorate.
The twin predicts the kernel's measurements closely (§7.4), so the routers the kernel
cannot run are still evaluated credibly.

**What we measured** — latency, packet loss and fairness against standard scheduling, the
three things the problem statement asks for (ranges over the six scenarios of §6;
reductions are per scenario):

| | Standard router | **FlowPilot** |
|---|---|---|
{{SUMMARY_ROWS}}
| Call packets lost (digital twin) | 0.02 – 0.11 % | **0.00 – 0.01 %** |
| Call video frames arriving too late (digital twin / real kernel) | 21 – 75 % / {{K_FIFO_LATE_T}} % | **0 – 1.4 % / {{K_FP_LATE_T}} %** |
| Netflix time rebuffering (digital twin) | up to 34 % | **0 %** |
| Fairness between devices, Jain index, 16 vs 1 connection (twin / kernel) | 0.57 / {{K_FIFO_JAIN}} | **0.98 / {{K_FP_JAIN}}** |
| QoE fairness F across all apps (twin / kernel) | 0.23 – 0.35 / {{K_FIFO_F}} | **0.71 – 0.85 / {{K_FP_F}}** |

Against the strongest open-source alternative (**CAKE + autorate**, which needs CAKE in the
kernel), FlowPilot is within 6 ms on latency when the ISP's capacity changes, keeps video
calls clean where fairness-only schedulers cannot (**33 vs 134 ms** call p99 and 0 % vs
1.8 % packet loss when the laptop on the call also syncs files; **37 vs 62 ms** under an
upload storm), streams video at up to **+32 %** higher bitrate under load, and runs on any
Linux router kernel — including the kernel of our own testbed, which has no fq_codel, CAKE
or netem. The price is paid by bulk transfers (typically ~20 % slower while interactive
traffic is active), which is the intended "lower-effort" behaviour.

**What is in the repository.** Python back-end (simulator, Linux testbed, controller,
ML/RL, FastAPI, SQLAlchemy/PostgreSQL), a React dashboard with a live **A/B mode — same
household, same congestion, standard router vs FlowPilot**, 15 automated tests, Docker
Compose, and scripts that reproduce every number in this report.

---

## Contents

1. [The problem, precisely](#1-the-problem-precisely)
2. [Research: state of the art and the gap we fill](#2-research-state-of-the-art-and-the-gap-we-fill)
3. [FlowPilot at a glance](#3-flowpilot-at-a-glance)
4. [Design in detail](#4-design-in-detail)
5. [Implementation](#5-implementation)
6. [Evaluation methodology](#6-evaluation-methodology)
7. [Results](#7-results)
8. [Demo script](#8-demo-script-5-minutes-for-the-judges)
9. [Deployment path and value](#9-deployment-path-and-value)
10. [Limitations and next steps](#10-limitations-and-what-we-would-do-next)
11. [How to run and reproduce](#11-how-to-run-and-reproduce)
12. [References](#12-references)
- [Appendix A — full simulator tables](#appendix-a--full-simulator-results)
- [Appendix B — full kernel tables](#appendix-b--full-real-kernel-results)

---

