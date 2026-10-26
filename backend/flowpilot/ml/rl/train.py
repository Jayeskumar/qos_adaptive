"""Train the autorate agent with Double DQN on the vectorised fluid access-link model.

    python -m flowpilot.ml.rl.train --steps 60000 --envs 32

State: 10 router-observable features (probe delay deltas, trend, shaper load, rate, time
since last bufferbloat, ...). Actions: multiply the shaper rate by one of
{0.70, 0.85, 0.95, 1.00, 1.02, 1.05, 1.10} every 200 ms. Reward: link utilisation minus
a delay penalty that grows quickly beyond ~10 ms of ISP queueing (see fluid.reward).
The safety shield is active during training, so the agent learns inside the same
envelope it is deployed with. Evaluation compares against the hand-tuned heuristic
(cake-autorate style) and a static 95 % shaper on held-out traces.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from ...controller.autorate import DelayAutorate
from .fluid import ACTIONS, DT, HOLD, SUB, FluidEnv, reward
from .policy import DEFAULT_WEIGHTS, OBS_DIM, DirFeat, QNet, shield

MAX_R, MIN_R = 0.95, 0.15


class Adam:
    def __init__(self, params: dict, lr: float = 5e-4, b1: float = 0.9, b2: float = 0.999, eps: float = 1e-8):
        self.p, self.lr, self.b1, self.b2, self.eps = params, lr, b1, b2, eps
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0

    def step(self, grads: dict) -> None:
        self.t += 1
        for k, g in grads.items():
            self.m[k] = self.b1 * self.m[k] + (1 - self.b1) * g
            self.v[k] = self.b2 * self.v[k] + (1 - self.b2) * g * g
            mh = self.m[k] / (1 - self.b1 ** self.t)
            vh = self.v[k] / (1 - self.b2 ** self.t)
            self.p[k] -= self.lr * mh / (np.sqrt(vh) + self.eps)


def grads_huber(net: QNet, x: np.ndarray, a: np.ndarray, y: np.ndarray, delta: float = 1.0):
    q, acts = net.forward(x, keep=True)
    B = x.shape[0]
    td = q[np.arange(B), a] - y
    dq = np.zeros_like(q)
    dq[np.arange(B), a] = np.clip(td, -delta, delta) / B
    loss = float(np.mean(np.where(np.abs(td) <= delta, 0.5 * td ** 2, delta * (np.abs(td) - 0.5 * delta))))
    g = {}
    up = dq
    for i in range(net.nl - 1, -1, -1):
        g[f"W{i}"] = acts[i].T @ up
        g[f"b{i}"] = up.sum(0)
        if i > 0:
            up = (up @ net.p[f"W{i}"].T) * (acts[i] > 0)
    # global gradient-norm clipping
    norm = np.sqrt(sum(float((v ** 2).sum()) for v in g.values()))
    if norm > 10.0:
        g = {k: v * (10.0 / norm) for k, v in g.items()}
    return g, loss


class Replay:
    def __init__(self, cap: int, dim: int) -> None:
        self.s = np.zeros((cap, dim), np.float32)
        self.a = np.zeros(cap, np.int64)
        self.r = np.zeros(cap, np.float32)
        self.s2 = np.zeros((cap, dim), np.float32)
        self.d = np.zeros(cap, np.float32)
        self.cap, self.n, self.i = cap, 0, 0

    def add(self, s, a, r, s2, d) -> None:
        k = len(a)
        idx = (self.i + np.arange(k)) % self.cap
        self.s[idx], self.a[idx], self.r[idx], self.s2[idx], self.d[idx] = s, a, r, s2, d
        self.i = (self.i + k) % self.cap
        self.n = min(self.cap, self.n + k)

    def sample(self, rng, b: int):
        idx = rng.integers(0, self.n, b)
        return self.s[idx], self.a[idx], self.r[idx], self.s2[idx], self.d[idx]


def _new_heur() -> DelayAutorate:
    h = DelayAutorate(max_frac=MAX_R)
    h.reset(0.95, 0.95, 1.0, 1.0)
    return h


def _observe(env: FluidEnv, feats: list, heur: list, probes: list, last_tx: np.ndarray):
    """Feed the interval's probes to features and to each env's heuristic; return the
    observation and the heuristic's proposed rate (the residual policy's base action)."""
    span = SUB * DT
    out = np.zeros((env.n, OBS_DIM), np.float32)
    prop = np.zeros(env.n)
    cut = np.zeros(env.n, dtype=bool)
    for i in range(env.n):
        for p in probes[i]:
            feats[i].ingest(p)
        achieved = (env.tx[i] - last_tx[i]) / span
        h = heur[i]
        h.dirs["down"].rate = env.R[i]  # the heuristic continues from the rate actually applied
        rd, _ = h.update(env.t[i] * DT, [(0.0, 5.0, p) for p in probes[i]], env.tx[i] / 8, env.tx[i] / 8, span)
        prop[i] = rd
        cut[i] = h.dirs["down"].last_action == "cut"
        out[i] = feats[i].obs(env.R[i], 1.0, achieved, env.t[i] * DT, rd / max(env.R[i], 1e-9))
    return out, prop, cut


def _select(net: QNet, obs: np.ndarray, feats: list, eps: float, rng, use_shield: bool = True) -> np.ndarray:
    q = net.forward(obs)
    a = q.argmax(1)
    explore = rng.random(len(a)) < eps
    a = np.where(explore, rng.integers(0, len(ACTIONS), len(a)), a)
    if use_shield:
        for i in range(len(a)):
            a[i] = shield(int(a[i]), feats[i].delta, float(obs[i, 3]))
    return a


def train(steps: int, n_envs: int, seed: int = 0, out: Path = DEFAULT_WEIGHTS, log_every: int = 2000,
          eval_every: int = 5000) -> dict:
    rng = np.random.default_rng(seed)
    env = FluidEnv(n_envs, seed)
    feats = [DirFeat() for _ in range(n_envs)]
    heur = [_new_heur() for _ in range(n_envs)]
    net = QNet(seed=seed)
    tgt = net.copy()
    opt = Adam(net.p, lr=5e-4)
    buf = Replay(300_000, OBS_DIM)
    gamma, batch, tau = 0.95, 256, 0.005
    last_tx = env.tx.copy()
    probes, _, _, _ = env.advance()
    obs, prop, cut = _observe(env, feats, heur, probes, last_tx)
    last_tx = env.tx.copy()
    hist = []
    curve = []
    ref = evaluate("heuristic", None, episodes=16, seed=4242)  # fixed held-out traces
    t0 = time.time()
    ep_ret = np.zeros(n_envs)
    rets: list = []
    for step in range(steps):
        eps = max(0.03, 1.0 - step / (0.6 * steps))
        a = _select(net, obs, feats, eps, rng)
        old_R = env.R.copy()
        env.R = np.clip(prop * ACTIONS[a], MIN_R, MAX_R)
        for i in range(n_envs):
            feats[i].note_action(int(a[i]), old_R[i], env.t[i] * DT, bool(cut[i]))
        probes, util, delay, loss = env.advance()
        r = reward(util, delay, loss, np.abs(env.R - old_R) > 0.02 * old_R)
        obs2, prop, cut = _observe(env, feats, heur, probes, last_tx)
        last_tx = env.tx.copy()
        done = env.done().astype(np.float32)
        buf.add(obs, a, r, obs2, done)
        ep_ret += r
        for i in np.where(done > 0)[0]:
            rets.append(ep_ret[i])
            ep_ret[i] = 0
            env.reset_one(i)
            feats[i] = DirFeat()
            heur[i] = _new_heur()
            last_tx[i] = 0.0
            prop[i] = env.R[i]
        obs = obs2
        if buf.n > 5_000:
            for _ in range(2):
                s, aa, rr, s2, dd = buf.sample(rng, batch)
                a2 = net.forward(s2).argmax(1)
                y = rr + gamma * (1 - dd) * tgt.forward(s2)[np.arange(batch), a2]
                g, loss_v = grads_huber(net, s, aa, y)
                opt.step(g)
                for k in tgt.p:
                    tgt.p[k] = (1 - tau) * tgt.p[k] + tau * net.p[k]
        if (step + 1) % eval_every == 0 or step == 0:
            e = evaluate("rl", net, episodes=16, seed=4242)
            curve.append({"step": step + 1, "transitions": (step + 1) * n_envs, "mean_reward": e["mean_reward"],
                          "utilization": e["utilization"], "frac_over_20ms": e["frac_over_20ms"]})
            print(f"  eval@{step + 1}: reward={e['mean_reward']:.3f} (heuristic {ref['mean_reward']:.3f}) "
                  f"util={e['utilization']:.3f} >20ms={e['frac_over_20ms'] * 100:.1f}%", flush=True)
        if (step + 1) % log_every == 0:
            mr = float(np.mean(rets[-50:])) if rets else float("nan")
            hist.append({"step": step + 1, "transitions": (step + 1) * n_envs, "eps": eps,
                         "episode_return": mr, "elapsed_s": time.time() - t0})
            print(f"step {step + 1}/{steps} eps={eps:.2f} return(last 50 ep)={mr:.1f} {time.time() - t0:.0f}s",
                  flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    net.save(out)
    return {"history": hist, "eval_curve": curve,
            "eval_reference": {k: ref[k] for k in ("mean_reward", "utilization", "frac_over_20ms")}}


# ------------------------------------------------------------------------- evaluation
def evaluate(policy: str, net: QNet | None, episodes: int = 64, seed: int = 999) -> dict:
    """Held-out traces: static 95 % shaper, the heuristic alone, heuristic + RL residual."""
    env = FluidEnv(episodes, seed)
    n = episodes
    feats = [DirFeat() for _ in range(n)]
    heur = [_new_heur() for _ in range(n)]
    rng = np.random.default_rng(0)
    last_tx = env.tx.copy()
    rewards, utils, delays = [], [], []
    probes, _, _, _ = env.advance()
    obs, prop, cut = _observe(env, feats, heur, probes, last_tx)
    last_tx = env.tx.copy()
    while not env.done().all():
        old = env.R.copy()
        if policy == "rl":
            a = _select(net, obs, feats, 0.0, rng)
            env.R = np.clip(prop * ACTIONS[a], MIN_R, MAX_R)
            for i in range(n):
                feats[i].note_action(int(a[i]), old[i], env.t[i] * DT, bool(cut[i]))
        elif policy == "heuristic":
            env.R = np.clip(prop, MIN_R, MAX_R)
            for i in range(n):
                feats[i].note_action(HOLD, old[i], env.t[i] * DT, bool(cut[i]))
        else:
            env.R = np.full(n, 0.95)
        changed = np.abs(env.R - old) > 0.02 * old
        probes, util, delay, loss = env.advance()
        r = reward(util, delay, loss, changed)
        obs, prop, cut = _observe(env, feats, heur, probes, last_tx)
        last_tx = env.tx.copy()
        rewards.append(r)
        utils.append(util)
        delays.append(delay)
    R = np.array(rewards)
    U = np.array(utils)
    D = np.array(delays)
    return {"policy": policy, "mean_reward": float(R.mean()), "utilization": float(U.mean()),
            "delay_mean_ms": float(D.mean()), "delay_p95_ms": float(np.percentile(D, 95)),
            "delay_p99_ms": float(np.percentile(D, 99)), "frac_over_20ms": float((D > 20).mean()),
            "by_kind": {k: {"utilization": float(U[:, env.kind == k].mean()),
                            "delay_p95_ms": float(np.percentile(D[:, env.kind == k], 95)),
                            "frac_over_20ms": float((D[:, env.kind == k] > 20).mean())}
                        for k in ("fixed", "fwa", "lte") if (env.kind == k).any()}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=60000)
    ap.add_argument("--envs", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--report", default="data/rl_report.json")
    ap.add_argument("--eval-only", action="store_true")
    a = ap.parse_args()
    rep = {}
    if not a.eval_only:
        rep["training"] = train(a.steps, a.envs, a.seed, Path(a.out))
    net = QNet.load(a.out)
    rep["eval"] = [evaluate("static", None), evaluate("heuristic", None), evaluate("rl", net)]
    for e in rep["eval"]:
        print(f"{e['policy']:10s} reward={e['mean_reward']:.3f} util={e['utilization']:.3f} "
              f"delay mean/p95/p99={e['delay_mean_ms']:.1f}/{e['delay_p95_ms']:.1f}/{e['delay_p99_ms']:.1f} ms "
              f">20ms={e['frac_over_20ms'] * 100:.1f}%  by kind={json.dumps(e['by_kind'])}")
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
