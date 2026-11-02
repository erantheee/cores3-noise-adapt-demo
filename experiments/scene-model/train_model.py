#!/usr/bin/env python3
"""Train an experimental scene head from source-disjoint, explicitly split NPZ data."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import torch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--version', required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error('output already exists; choose a new version/artifact')
    with np.load(args.dataset, allow_pickle=False) as data:
        x = data['X'].astype(np.float32)
        y = data['y'].astype(np.int64)
        groups = data['source_id'].astype(str)
        split = data['split'].astype(str)
        classes = data['classes'].astype(str).tolist()
    if classes != ['street', 'highway', 'market']:
        p.error('classes must be street / highway / market, in that order')
    if x.ndim != 2 or x.shape[1] != 92 or not np.isfinite(x).all():
        p.error('X must be finite N x 92 features captured from S3')
    if any(a.shape != (len(x),) for a in (y, groups, split)) or not len(x):
        p.error('y, source_id and split must have one entry per window')
    if not set(y.tolist()) <= {0, 1, 2} or any(not g.strip() for g in groups):
        p.error('invalid labels or empty source identifiers')
    names = ('train', 'validation', 'test')
    if set(split.tolist()) != set(names):
        p.error('split must contain train / validation / test')
    sets = {s: set(groups[split == s].tolist()) for s in names}
    if any(sets[a] & sets[b] for i, a in enumerate(names) for b in names[i+1:]):
        p.error('recording/source leakage between splits')
    if any(set(y[split == s].tolist()) != {0, 1, 2} for s in names):
        p.error('each split must contain all three classes')
    shape = x.copy()
    shape[:, :24] -= x[:, 72:73]
    train = split == 'train'
    mu = shape[train].mean(0)
    sd = shape[train].std(0)
    sd[sd < 1e-3] = 1.0
    z = (shape - mu) / sd
    torch.manual_seed(7)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    model = torch.nn.Linear(92, 3)
    opt = torch.optim.Adam(model.parameters(), lr=0.02, weight_decay=1e-4)
    xt = torch.tensor(z[train]); yt = torch.tensor(y[train])
    for _ in range(500):
        opt.zero_grad()
        loss = torch.nn.functional.cross_entropy(model(xt), yt)
        loss.backward(); opt.step()
    w = model.weight.detach().numpy(); b = model.bias.detach().numpy()
    logits = z @ w.T + b
    reports = {}
    for name in names:
        mask = split == name
        cm = np.zeros((3, 3), dtype=int)
        for truth, pred in zip(y[mask], logits[mask].argmax(1)):
            cm[truth, pred] += 1
        per_class = {}
        for i, label in enumerate(classes):
            precision = float(cm[i, i] / max(1, cm[:, i].sum()))
            recall = float(cm[i, i] / max(1, cm[i].sum()))
            per_class[label] = dict(precision=precision, recall=recall,
                                   f1=2*precision*recall/max(1e-12, precision+recall))
        reports[name] = dict(windows=int(mask.sum()), sources=sorted(sets[name]),
                             confusion=cm.tolist(), per_class=per_class,
                             macro_f1=sum(v['f1'] for v in per_class.values()) / 3)
    golden = []
    for i, label in enumerate(classes):
        for row in np.flatnonzero((split == 'test') & (y == i))[:3]:
            golden.append(dict(label=label, vector=x[row].tolist(),
                               logits=logits[row].tolist(), argmax=int(logits[row].argmax())))
    artifact = dict(version=args.version, feature_count=92, classes=classes,
                    normalization=dict(mean=mu.tolist(), std=sd.tolist()),
                    weights=w.tolist(), bias=b.tolist(),
                    folded_weights=(w / sd).reshape(-1).tolist(),
                    folded_bias=(b-(w*(mu/sd)).sum(1)).tolist(), golden=golden,
                    provenance=dict(status='experimental; no automatic deployment',
                                    dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                                    seed=7, epochs=500, numpy=np.__version__, torch=torch.__version__,
                                    split_policy='source-disjoint; normalization and fitting on train only'),
                    evaluation=reports)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
