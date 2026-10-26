"""Generate a labelled flow-feature dataset from randomised simulated households.

Each episode draws a random household: link capacity (8-300 Mb/s down), Internet path
delays (2-90 ms), a random queue management (drop-tail with bufferbloat, fq_codel, static
QoS) and 3-9 applications with randomised parameters (tick rates, bitrates, segment
lengths, connection counts, think times...). Features are taken by the router-side
FlowMonitor - exactly the code the live controller uses - at several flow ages
(0.5 s ... 60 s) so the model learns to classify early.

    python -m flowpilot.ml.dataset --episodes 400 --out data/flows.npz
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import random
import time
from pathlib import Path

import numpy as np

from ..common.classes import APP_TYPES, App
from ..common.features import FEATURE_NAMES, feature_vector
from ..sim.apps import BulkApp, GamingApp, StreamingApp, VideoCallApp, WebApp
from ..sim.baselines import make_baseline
from ..sim.world import DEFAULT_DEVICES, HomeWorld, LinkProfile, Server

SAMPLE_AGES = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0, 45.0, 60.0]


def _episode(args: tuple[int, float]) -> tuple[list, list, list]:
    seed, duration = args
    rng = random.Random(seed)
    down = rng.choice([8, 12, 20, 30, 40, 50, 75, 100, 150, 300]) * rng.uniform(0.8, 1.2)
    up = max(1.0, down * rng.choice([0.1, 0.2, 0.25, 0.5, 1.0]))
    link = LinkProfile(down_mbps=down, up_mbps=up, isp_buffer_ms_down=rng.uniform(30, 600),
                       isp_buffer_ms_up=rng.uniform(30, 1200), access_delay_ms=rng.uniform(1, 12),
                       lan_delay_ms=rng.uniform(0.2, 4))
    servers = [Server(n, ip, rng.uniform(2, 90)) for n, ip in
               [("game", "203.0.113.10"), ("call", "203.0.113.11"), ("cdn", "203.0.113.12"),
                ("mirror", "203.0.113.13"), ("web", "203.0.113.14"), ("backup", "203.0.113.15"),
                ("reflector", "203.0.113.53")]]
    qos = make_baseline(rng.choice(["fifo", "fifo", "fq_codel", "static_qos", "cake"]))
    w = HomeWorld(link, qos, seed=seed, servers=servers)
    devs = [d.name for d in DEFAULT_DEVICES]
    n_apps = rng.randint(3, 9)
    kinds = [rng.choice([App.GAMING, App.VIDEO_CALL, App.STREAMING, App.WEB, App.WEB, App.BULK, App.UPLOAD])
             for _ in range(n_apps)]
    for i, k in enumerate(kinds):
        dev = rng.choice(devs)
        start = rng.uniform(0, duration * 0.5)
        stop = start + rng.uniform(10, duration) if rng.random() < 0.3 else None
        aid = f"{k}{i}"
        if k == App.GAMING:
            w.add_app(GamingApp, aid, dev, "game", start=start, stop=stop, randomize=True,
                      port=rng.randint(1024, 65000))
        elif k == App.VIDEO_CALL:
            w.add_app(VideoCallApp, aid, dev, "call", start=start, stop=stop, randomize=True,
                      port=rng.randint(1024, 65000))
        elif k == App.STREAMING:
            w.add_app(StreamingApp, aid, dev, "cdn", start=start, stop=stop, randomize=True)
        elif k == App.WEB:
            w.add_app(WebApp, aid, dev, "web", start=start, stop=stop, randomize=True)
        elif k == App.BULK:
            w.add_app(BulkApp, aid, dev, "mirror", start=start, stop=stop, randomize=True)
        else:
            w.add_app(BulkApp, aid, dev, "backup", start=start, stop=stop, randomize=True, upload=True)
    X: list = []
    y: list = []
    meta: list = []
    sampled: dict = {}
    t = 0.0
    while t < duration:
        t += 0.5
        w.run(t, step=0.1)
        mon = w.monitor
        per_host, per_dst = mon.host_counts(t)
        for key, fs in list(mon.flows.items()):
            if fs.pkts < 6 or t - fs.last_ts > 2.0:
                continue
            age = t - fs.first_ts
            done = sampled.setdefault(key, set())
            due = [a for a in SAMPLE_AGES if a <= age + 1e-9 and a not in done]
            if not due:
                continue
            done.update(due)
            f = w.flow_by_key.get(key)
            if f is None or f.app_type not in APP_TYPES:
                continue
            vec, _ = feature_vector(fs, t, per_host.get(fs.lan_ip, 1), per_dst.get((fs.lan_ip, fs.wan_ip), 1))
            X.append(vec)
            y.append(APP_TYPES.index(f.app_type))
            meta.append((seed, round(age, 2)))
        mon.expire(t)
    return X, y, meta


def generate(episodes: int, duration: float = 50.0, seed0: int = 1000, workers: int = 0):
    workers = workers or max(1, mp.cpu_count())
    jobs = [(seed0 + i, duration) for i in range(episodes)]
    X, y, meta = [], [], []
    t0 = time.time()
    with mp.Pool(workers) as pool:
        for i, (xs, ys, ms) in enumerate(pool.imap_unordered(_episode, jobs)):
            X.extend(xs)
            y.extend(ys)
            meta.extend(ms)
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{episodes} episodes, {len(X)} samples, {time.time() - t0:.0f}s", flush=True)
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int16), np.asarray(meta, dtype=np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=400)
    ap.add_argument("--duration", type=float, default=50.0)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--out", default="data/flows_sim.npz")
    a = ap.parse_args()
    X, y, meta = generate(a.episodes, a.duration, a.seed)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(a.out, X=X, y=y, meta=meta, features=np.array(FEATURE_NAMES), labels=np.array(APP_TYPES))
    counts = {APP_TYPES[i]: int((y == i).sum()) for i in range(len(APP_TYPES))}
    print(f"saved {len(y)} samples to {a.out}: {counts}")


if __name__ == "__main__":
    main()
