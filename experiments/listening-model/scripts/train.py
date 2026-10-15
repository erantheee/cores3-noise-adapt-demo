"""训练端侧听音情境模型（v0.1，MLP）。

输入：92 维窗特征（最近 2 秒的 24 频段均值/标准差/斜率 + 电平 + 语音描述量 + 时间）
输出：9 类情境 softmax

为什么是 MLP 而不是 CNN：特征已经是固件算好的 24 频段 dB 值，时间结构由均值/标准差/斜率
三个统计量承担。端侧新增的只有一次 92→128→64→9 的前向（约 2.1 万 MACs），不引入任何新
DSP，也不需要在设备上重实现梅尔滤波。

用法：
    python scripts/train.py --data artifacts/dataset_synth.npz --out artifacts/model_v0.1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn


class ContextNet(nn.Module):
    def __init__(self, n_in: int, n_classes: int, hidden=(128, 64), dropout=0.2):
        super().__init__()
        layers = []
        prev = n_in
        for h in hidden:
            layers += [nn.Linear(prev, h), nn.ReLU(), nn.Dropout(dropout)]
            prev = h
        layers += [nn.Linear(prev, n_classes)]
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


def augment(x: torch.Tensor, rng: torch.Generator, noise=0.05, drop=0.05):
    x = x + torch.randn(x.shape, generator=rng) * noise
    mask = (torch.rand(x.shape, generator=rng) > drop).float()
    return x * mask


def evaluate(model, x, y):
    model.eval()
    with torch.no_grad():
        logits = model(x)
        pred = logits.argmax(dim=1)
    y_np = y.numpy()
    p_np = pred.numpy()
    acc = float((p_np == y_np).mean()) if len(y_np) else float("nan")
    return acc, p_np, y_np


def confusion(y_true, y_pred, n_classes):
    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        cm[t, p] += 1
    return cm


def report(cm, classes):
    out = {}
    for i, name in enumerate(classes):
        tp = cm[i, i]
        fp = cm[:, i].sum() - tp
        fn = cm[i, :].sum() - tp
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[name] = {"precision": round(prec, 4), "recall": round(rec, 4),
                     "f1": round(f1, 4), "support": int(cm[i, :].sum())}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--holdout", default=None, help="额外的独立验证集 npz（例如 real 语料）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--patience", type=int, default=30)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--version", default="v0.1.0")
    ap.add_argument("--hidden", type=int, nargs=2, default=(128, 64),
                    metavar=("H1", "H2"), help="两层隐藏层宽度（S3 体积/精度扫描用）")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    rng = torch.Generator().manual_seed(args.seed)

    d = np.load(args.data, allow_pickle=True)
    classes = [str(c) for c in d["classes"]]
    mu, sigma = d["mu"].astype(np.float32), d["sigma"].astype(np.float32)
    X_tr = (d["X_train"] - mu) / sigma
    X_va = (d["X_val"] - mu) / sigma
    X_tr, y_tr = torch.tensor(X_tr), torch.tensor(d["y_train"])
    X_va, y_va = torch.tensor(X_va), torch.tensor(d["y_val"])

    hidden = tuple(args.hidden)
    model = ContextNet(X_tr.shape[1], len(classes), hidden=hidden)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    loss_fn = nn.CrossEntropyLoss(label_smoothing=0.05)

    best = {"val_loss": float("inf"), "state": None, "epoch": -1}
    batch = 64
    for epoch in range(args.epochs):
        model.train()
        perm = torch.randperm(len(X_tr), generator=rng)
        total = 0.0
        for i in range(0, len(perm), batch):
            idx = perm[i:i + batch]
            xb = augment(X_tr[idx], rng)
            yb = y_tr[idx]
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            opt.step()
            total += float(loss.detach()) * len(idx)
        sched.step()
        model.eval()
        with torch.no_grad():
            val_loss = float(loss_fn(model(X_va), y_va))
        if val_loss < best["val_loss"]:
            best = {"val_loss": val_loss, "epoch": epoch,
                    "state": {k: v.clone() for k, v in model.state_dict().items()}}
        if epoch - best["epoch"] > args.patience:
            break

    model.load_state_dict(best["state"])
    acc_tr, _, _ = evaluate(model, X_tr, y_tr)
    acc_va, pred_va, y_va_np = evaluate(model, X_va, y_va)
    cm = confusion(y_va_np, pred_va, len(classes))

    result = {
        "version": args.version,
        "model": f"ContextNet (MLP {X_tr.shape[1]}-{hidden[0]}-{hidden[1]}-{len(classes)})",
        "hidden": list(hidden),
        "parameters": int(sum(p.numel() for p in model.parameters())),
        "macs_per_inference": int(X_tr.shape[1] * hidden[0]
                                  + hidden[0] * hidden[1]
                                  + hidden[1] * len(classes)),
        "weights_bytes_f32": int(sum(p.numel() for p in model.parameters()) * 4),
        "classes": classes,
        "epochs_run": best["epoch"],
        "train_accuracy": round(acc_tr, 4),
        "val_accuracy": round(acc_va, 4),
        "val_confusion": cm.tolist(),
        "per_class": report(cm, classes),
        "val_samples": int(len(y_va_np)),
        "train_samples": int(len(y_tr)),
    }

    if args.holdout:
        h = np.load(args.holdout, allow_pickle=True)
        X_h_raw = h["X_val"] if len(h["X_val"]) else h["X_train"]
        y_h_raw = h["y_val"] if len(h["y_val"]) else h["y_train"]
        X_h = torch.tensor((X_h_raw - mu) / sigma)
        y_h = torch.tensor(y_h_raw)
        if len(X_h):
            acc_h, pred_h, y_h_np = evaluate(model, X_h, y_h)
            cm_h = confusion(y_h_np, pred_h, len(classes))
            result["holdout_accuracy"] = round(acc_h, 4)
            result["holdout_confusion"] = cm_h.tolist()
            result["holdout_per_class"] = report(cm_h, classes)
            result["holdout_note"] = "留出音色/语料，训练从未见过"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict": model.state_dict(),
        "mu": mu, "sigma": sigma,
        "classes": classes,
        "feature_names": [str(x) for x in d["feature_names"]],
        "metrics": result,
    }, Path(str(out) + ".pt"))
    Path(str(out) + ".report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2))

    print(json.dumps({k: result[k] for k in ("train_accuracy", "val_accuracy")}, indent=2))
    print("每类：")
    for name, m in result["per_class"].items():
        print(f"  {name:12s} P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} n={m['support']}")
    if "holdout_accuracy" in result:
        print(f"留出集准确率：{result['holdout_accuracy']:.3f}")
    print("验证集混淆矩阵（行=真实，列=预测）：")
    print("      " + " ".join(f"{c[:5]:>6s}" for c in classes))
    for i, c in enumerate(classes):
        print(f"{c[:5]:>5s} " + " ".join(f"{v:6d}" for v in cm[i]))


if __name__ == "__main__":
    main()
