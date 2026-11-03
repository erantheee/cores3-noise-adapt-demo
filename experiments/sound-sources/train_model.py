#!/usr/bin/env python3
"""Train a 92 -> 24 -> 7 multi-label baseline. NumPy only; no deployment."""
import argparse
import hashlib
from pathlib import Path

import numpy as np

from pipeline import CLASSES, FEATURE_VERSION, SPLITS, validate_dataset, write_json


def sigmoid(x):
    e = np.exp(-np.abs(x))
    return np.where(x >= 0, 1 / (1 + e), e / (1 + e))


def metrics(y, scores, threshold):
    known = y >= 0
    truth = y[known] == 1
    pred = scores[known] >= threshold
    tp, fp = int((truth & pred).sum()), int((~truth & pred).sum())
    fn, tn = int((truth & ~pred).sum()), int((~truth & ~pred).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    fpr = fp / (fp + tn) if fp + tn else None
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=precision, recall=recall,
                f1=2*precision*recall / max(precision+recall, 1e-12), fpr=fpr)


def select_threshold(y, scores):
    candidates = []
    for threshold in np.arange(0.05, 0.951, 0.01):
        m = metrics(y, scores, threshold)
        acceptable = m["fpr"] is not None and m["fpr"] <= 0.1 and m["precision"] >= 0.8
        candidates.append((acceptable, m["recall"] if acceptable else m["f1"],
                           -(m["fpr"] or 0), float(threshold)))
    return max(candidates)[-1]


def duration(data, mask):
    # Union overlapping windows within each session, never count a 2 s window four times.
    total = 0.0
    for session in set(data["session_id"][mask]):
        take = mask & (data["session_id"] == session)
        intervals = sorted(zip(data["start_s"][take], data["end_s"][take]))
        end = -1.0
        for a, b in intervals:
            total += max(0.0, float(b) - max(float(a), end))
            end = max(end, float(b))
    return round(total, 4)


def evaluate(data, scores, thresholds, trained):
    y = data["y"]
    mixed = (y == 1).sum(1) >= 2
    single = ((y == 1).sum(1) == 1) & (y >= 0).all(1)
    reports = {}
    for split in SPLITS:
        base = data["split"] == split
        reports[split] = {}
        for scope, subset in (("all", np.ones(len(y), dtype=bool)), ("mixed", mixed), ("single", single)):
            per_class = {}
            for c, name in enumerate(CLASSES):
                take = base & subset
                report = metrics(y[take, c], scores[take, c], thresholds[c]) if trained[c] else None
                # Only real S3 recordings contribute to offline acceptance coverage/metrics.
                real = take & (data["recording_kind"] == "real_s3")
                real_metrics = metrics(y[real, c], scores[real, c], thresholds[c]) if trained[c] else None
                pos, neg = real & (y[:, c] == 1), real & (y[:, c] == 0)
                coverage = dict(positive_s=duration(data, pos), negative_s=duration(data, neg),
                                sessions=len(set(data["session_id"][pos | neg])),
                                positive_sessions=len(set(data["session_id"][pos])),
                                negative_sessions=len(set(data["session_id"][neg])))
                sufficient = (trained[c] and coverage["positive_s"] >= 60
                              and coverage["negative_s"] >= 60 and coverage["sessions"] >= 3)
                passed = (sufficient and real_metrics["precision"] >= 0.8
                          and real_metrics["recall"] >= 0.8 and real_metrics["fpr"] <= 0.1)
                status = "data_insufficient" if not sufficient else "offline_pass" if passed else "offline_failed"
                per_class[name] = dict(metrics=report, real_s3_metrics=real_metrics,
                                       coverage=coverage, status=status)
            reports[split][scope] = per_class
    return reports


def fit(data, epochs=500):
    x, y = validate_dataset(data)
    if set(data["split"]) != set(SPLITS):
        raise ValueError("training requires nonempty train/validation/test splits")
    train, val = data["split"] == "train", data["split"] == "validation"
    trained = np.array([set(y[train, c]) >= {0, 1} and set(y[val, c]) >= {0, 1} for c in range(7)])
    if not trained.any():
        raise ValueError("no class has both positive and negative human labels in train and validation")
    mean = x[train].mean(0)
    scale = x[train].std(0)
    scale[scale < 1e-3] = 1
    z = ((x - mean) / scale).astype(np.float32)
    if not np.isfinite(z).all():
        raise ValueError("normalization overflow")
    rng = np.random.default_rng(7)
    params = [rng.normal(0, np.sqrt(2/92), (24, 92)).astype(np.float32),
              np.zeros(24, dtype=np.float32),
              rng.normal(0, np.sqrt(1/24), (7, 24)).astype(np.float32),
              np.zeros(7, dtype=np.float32)]
    moment = [np.zeros_like(p) for p in params]
    variance = [np.zeros_like(p) for p in params]
    xt, yt = z[train], y[train]
    mask = (yt >= 0) & trained[None, :]
    target = np.maximum(yt, 0)
    for step in range(1, epochs+1):
        w1, b1, w2, b2 = params
        hidden = np.maximum(0, xt @ w1.T + b1)
        output = sigmoid(hidden @ w2.T + b2)
        grad = (output - target) * mask / mask.sum()
        dh = (grad @ w2) * (hidden > 0)
        grads = [dh.T @ xt, dh.sum(0), grad.T @ hidden, grad.sum(0)]
        for i, (p, g) in enumerate(zip(params, grads)):
            g = g + (1e-4 * p if p.ndim == 2 else 0)
            moment[i] = 0.9*moment[i] + 0.1*g
            variance[i] = 0.999*variance[i] + 0.001*g*g
            p -= 0.01*(moment[i]/(1-0.9**step)) / (np.sqrt(variance[i]/(1-0.999**step)) + 1e-8)
    w1, b1, w2, b2 = params
    scores = sigmoid(np.maximum(0, z @ w1.T + b1) @ w2.T + b2)
    if not all(np.isfinite(p).all() for p in params) or not np.isfinite(scores).all():
        raise ValueError("non-finite fit")
    thresholds = [select_threshold(y[val, c], scores[val, c]) if trained[c] else 0.5 for c in range(7)]
    reports = evaluate(data, scores, thresholds, trained)
    statuses = {}
    for c, name in enumerate(CLASSES):
        tests = [reports["test"][scope][name]["status"] for scope in ("all", "mixed")]
        statuses[name] = ("data_insufficient" if "data_insufficient" in tests else
                          "offline_pass" if all(s == "offline_pass" for s in tests) else "offline_failed")
    artifact = dict(schema=1, version="", feature_version=FEATURE_VERSION,
                    classes=CLASSES, architecture=[92, 24, 7],
                    normalization=dict(mean=mean.tolist(), scale=scale.tolist()),
                    w1=w1.reshape(-1).tolist(), b1=b1.tolist(),
                    w2=w2.reshape(-1).tolist(), b2=b2.tolist(),
                    trained=trained.tolist(), thresholds=thresholds,
                    field_validated=[False]*7, quiet_threshold=None,
                    evaluation=reports, acceptance=statuses,
                    golden=[dict(vector=x[i].tolist(), scores=scores[i].tolist())
                            for i in np.flatnonzero(data["split"] == "test")[:8]],
                    provenance=dict(seed=7, epochs=epochs, numpy=np.__version__,
                                    split_policy="source/session disjoint; train-only normalization/fit; validation-only thresholds",
                                    status="experimental telemetry only; independent S3 field acceptance pending"))
    return artifact


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--version", required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error("output exists; choose a new version")
    try:
        with np.load(args.dataset, allow_pickle=False) as file:
            data = {key: file[key] for key in file.files}
        artifact = fit(data)
        artifact["version"] = args.version
        artifact["provenance"]["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
        write_json(args.output, artifact)
        for name, status in artifact["acceptance"].items():
            print(f"{name}: {status}; field validation pending")
    except (ValueError, KeyError, OSError) as exc:
        p.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
