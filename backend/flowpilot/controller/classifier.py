"""Flow classifier: behavioural features -> application type (+ confidence + explanation).

The production model is a gradient-boosted tree ensemble trained by
`flowpilot.ml.train_classifier` on labelled flows from the simulator and the Linux testbed
(see docs/REPORT.md, "Learning which applications need bandwidth"). If no trained model is
available a transparent rule-based prior is used, so the controller always works.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Inference runs on small batches (tens of flows) on a router-class CPU: one thread is
# fastest, and it prevents OpenMP thread oversubscription when several simulations run.
os.environ.setdefault("OMP_NUM_THREADS", "1")

from ..common.classes import APP_LABELS, APP_TYPES, App
from ..common.features import FEATURE_NAMES

MODEL_DIR = Path(os.environ.get("FLOWPILOT_MODEL_DIR", Path(__file__).resolve().parents[1] / "models"))
DEFAULT_MODEL = MODEL_DIR / "flow_classifier.joblib"
F = {n: i for i, n in enumerate(FEATURE_NAMES)}


def rule_based(vec: list[float]) -> tuple[str, float]:
    """Expert prior derived from the published characteristics of each application class."""
    import math

    udp = vec[F["is_udp"]] > 0.5
    kup = math.expm1(vec[F["log_kbps_up"]])
    kdown = math.expm1(vec[F["log_kbps_down"]])
    pps_up = math.expm1(vec[F["log_pps_up"]])
    pps_down = math.expm1(vec[F["log_pps_down"]])
    age = math.expm1(vec[F["log_age_s"]])
    if udp:
        if pps_up > 10 and pps_down > 10 and vec[F["mean_size_up"]] < 260 and kdown < 1500 and kup < 600:
            return App.GAMING, 0.75
        if kdown > 250 or kup > 250:
            return App.VIDEO_CALL, 0.7
        return App.GAMING, 0.5
    if kup > 800 and vec[F["log_ratio_up_down"]] > 2.0:
        return App.UPLOAD, 0.75
    if vec[F["burst_cv_down"]] > 1.2 and vec[F["on_frac_down"]] < 0.75 and age > 6:
        return App.STREAMING, 0.6
    if math.expm1(vec[F["log_host_same_dst"]]) >= 2.5 and kdown > 800:
        return App.BULK, 0.7
    if kdown > 4000 and age > 4:
        return App.BULK, 0.55
    return App.WEB, 0.5


class FlowClassifier:
    def __init__(self, model_path: Optional[str | Path] = None) -> None:
        self.model = None
        self.labels = list(APP_TYPES)
        self.card: dict = {}
        path = Path(model_path) if model_path else DEFAULT_MODEL
        if path.exists():
            try:
                import joblib

                bundle = joblib.load(path)
                self.model = bundle["model"]
                self.labels = list(bundle["labels"])
                self.card = bundle.get("card", {})
            except Exception as exc:  # pragma: no cover - corrupted model falls back to rules
                self.card = {"load_error": str(exc)}
        self.kind = "gbdt" if self.model is not None else "rules"

    def predict(self, vecs: list[list[float]]) -> list[tuple[str, float, dict]]:
        if not vecs:
            return []
        if self.model is None:
            out = []
            for v in vecs:
                lab, conf = rule_based(v)
                out.append((lab, conf, {lab: conf}))
            return out
        import numpy as np
        from threadpoolctl import threadpool_limits

        with threadpool_limits(limits=1):
            P = self.model.predict_proba(np.asarray(vecs, dtype=float))
        out = []
        for row in P:
            i = int(row.argmax())
            probs = {self.labels[j]: float(row[j]) for j in range(len(self.labels))}
            out.append((self.labels[i], float(row[i]), probs))
        return out

    @staticmethod
    def explain(label: str, info: dict) -> str:
        k_up, k_dn = info.get("kbps_up", 0.0), info.get("kbps_down", 0.0)
        pu, pd = info.get("pps_up", 0.0), info.get("pps_down", 0.0)
        su, sd = info.get("mean_size_up", 0.0), info.get("mean_size_down", 0.0)
        if label == App.GAMING:
            return (f"steady {pu:.0f}/{pd:.0f} pkt/s of small packets ({su:.0f}/{sd:.0f} B), "
                    f"only {k_up + k_dn:.0f} kb/s: a game-state exchange")
        if label == App.VIDEO_CALL:
            return (f"symmetric real-time media {k_up / 1000:.1f}/{k_dn / 1000:.1f} Mb/s up/down "
                    f"with paced frames: interactive video")
        if label == App.STREAMING:
            return (f"ON/OFF downloads (burstiness {info.get('burst_cv_down', 0):.1f}, on {info.get('on_frac_down', 0) * 100:.0f}% "
                    f"of the time) of large segments: buffered video")
        if label == App.BULK:
            return (f"sustained {k_dn / 1000:.1f} Mb/s of full-size packets over "
                    f"{info.get('host_same_dst', 1)} parallel connections: a bulk download")
        if label == App.UPLOAD:
            return f"sustained {k_up / 1000:.1f} Mb/s upstream: cloud backup / upload"
        return f"short request/response bursts ({k_dn / 1000:.1f} Mb/s): interactive web traffic"

    @staticmethod
    def label_name(label: str) -> str:
        return APP_LABELS.get(label, label)
