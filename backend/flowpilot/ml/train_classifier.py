"""Train and evaluate the FlowPilot traffic classifier.

    python -m flowpilot.ml.train_classifier --data data/flows_sim.npz [--extra data/flows_testbed.npz]

* Grouped split: whole households (episodes) are held out, so the score measures
  generalisation to unseen homes, not memorisation of flows.
* Model: histogram gradient-boosted trees (fast to train, ~1 ms inference for a batch of
  flows on a router CPU, robust to unscaled features) with class re-weighting.
* Reported: accuracy, macro-F1, per-class precision/recall, confusion matrix, accuracy as a
  function of flow age (how quickly a new flow can be classified), permutation feature
  importance, and comparison with a random forest and the rule-based prior.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.utils.class_weight import compute_sample_weight

from ..common.classes import APP_TYPES
from ..common.features import FEATURE_NAMES
from ..controller.classifier import DEFAULT_MODEL, rule_based

AGE_BUCKETS = [(0, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 16.0), (16.0, 1e9)]


def load(paths: list[str]):
    Xs, ys, ms = [], [], []
    for p in paths:
        d = np.load(p)
        Xs.append(d["X"])
        ys.append(d["y"])
        m = d["meta"]
        if Path(p).name.startswith("flows_testbed"):
            m = m.copy()
            m[:, 0] += 10_000_000  # testbed episodes form their own groups
        ms.append(m)
    return np.vstack(Xs), np.concatenate(ys), np.vstack(ms)


def grouped_split(meta: np.ndarray, test_frac: float = 0.2, seed: int = 0):
    groups = np.unique(meta[:, 0])
    rng = np.random.default_rng(seed)
    test_groups = set(rng.choice(groups, size=max(1, int(len(groups) * test_frac)), replace=False).tolist())
    is_test = np.array([g in test_groups for g in meta[:, 0]])
    return ~is_test, is_test


def make_model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=400, learning_rate=0.08, max_leaf_nodes=48,
                                          min_samples_leaf=30, l2_regularization=1e-3,
                                          early_stopping=True, validation_fraction=0.1,
                                          n_iter_no_change=25, random_state=0)


def evaluate(model, X, y, meta) -> dict:
    pred = model.predict(X)
    labels = list(range(len(APP_TYPES)))
    rep = classification_report(y, pred, labels=labels, target_names=APP_TYPES, output_dict=True, zero_division=0)
    by_age = []
    for lo, hi in AGE_BUCKETS:
        m = (meta[:, 1] > lo) & (meta[:, 1] <= hi)
        if m.sum():
            by_age.append({"age_lo": lo, "age_hi": hi if hi < 1e8 else None, "n": int(m.sum()),
                           "accuracy": float(accuracy_score(y[m], pred[m]))})
    return {
        "accuracy": float(accuracy_score(y, pred)),
        "macro_f1": float(f1_score(y, pred, average="macro")),
        "per_class": {k: {"precision": rep[k]["precision"], "recall": rep[k]["recall"], "f1": rep[k]["f1-score"],
                          "support": rep[k]["support"]} for k in APP_TYPES},
        "confusion": confusion_matrix(y, pred, labels=labels).tolist(),
        "by_age": by_age,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", default=["data/flows_sim.npz"])
    ap.add_argument("--out", default=str(DEFAULT_MODEL))
    ap.add_argument("--report", default="data/classifier_report.json")
    a = ap.parse_args()
    X, y, meta = load(a.data)
    tr, te = grouped_split(meta)
    print(f"{len(y)} samples, train {tr.sum()} / test {te.sum()} (households held out)")
    w = compute_sample_weight("balanced", y[tr])
    t0 = time.time()
    model = make_model()
    model.fit(X[tr], y[tr], sample_weight=w)
    train_s = time.time() - t0
    res = evaluate(model, X[te], y[te], meta[te])
    t0 = time.time()
    model.predict_proba(X[te][:64])
    infer_ms = (time.time() - t0) * 1000
    print(f"GBDT: accuracy {res['accuracy']:.4f} macro-F1 {res['macro_f1']:.4f} (train {train_s:.1f}s)")
    # ---- baselines for the report
    rf = RandomForestClassifier(n_estimators=200, min_samples_leaf=3, n_jobs=-1, class_weight="balanced",
                                random_state=0).fit(X[tr], y[tr])
    rf_res = evaluate(rf, X[te], y[te], meta[te])
    rule_pred = np.array([APP_TYPES.index(rule_based(list(v))[0]) for v in X[te]])
    rule_acc = float(accuracy_score(y[te], rule_pred))
    rule_f1 = float(f1_score(y[te], rule_pred, average="macro"))
    print(f"RandomForest: {rf_res['accuracy']:.4f} / {rf_res['macro_f1']:.4f}; rules: {rule_acc:.4f} / {rule_f1:.4f}")
    # ---- permutation importance on a subsample
    rng = np.random.default_rng(0)
    idx = rng.choice(np.where(te)[0], size=min(6000, int(te.sum())), replace=False)
    pi = permutation_importance(model, X[idx], y[idx], n_repeats=4, random_state=0, scoring="accuracy")
    imp = sorted(({"feature": FEATURE_NAMES[i], "importance": float(pi.importances_mean[i])}
                  for i in range(len(FEATURE_NAMES))), key=lambda d: -d["importance"])
    card = {
        "model": "HistGradientBoostingClassifier", "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_samples": int(len(y)), "n_train": int(tr.sum()), "n_test": int(te.sum()),
        "n_households": int(len(np.unique(meta[:, 0]))), "labels": APP_TYPES, "features": FEATURE_NAMES,
        "test": res, "random_forest": {k: rf_res[k] for k in ("accuracy", "macro_f1")},
        "rule_based": {"accuracy": rule_acc, "macro_f1": rule_f1},
        "importance": imp, "train_seconds": train_s, "inference_ms_per_64_flows": infer_ms,
        "class_counts": {APP_TYPES[i]: int((y == i).sum()) for i in range(len(APP_TYPES))},
        "data": a.data,
    }
    # refit on everything for deployment
    final = make_model().fit(X, y, sample_weight=compute_sample_weight("balanced", y))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": final, "labels": APP_TYPES, "features": FEATURE_NAMES, "card": card}, a.out)
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(card, indent=2))
    print(f"saved model -> {a.out}; report -> {a.report}")
    print("top features:", ", ".join(f"{d['feature']} {d['importance']:.3f}" for d in imp[:8]))
    print("accuracy by flow age:", [(b["age_lo"], round(b["accuracy"], 3)) for b in res["by_age"]])


if __name__ == "__main__":
    main()
