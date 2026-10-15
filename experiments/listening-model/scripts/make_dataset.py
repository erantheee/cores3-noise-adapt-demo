"""把合成/真实 WAV 语料转成模型数据集（.npz）。

特征在"设备口径"下提取：48 kHz、2560 样本帧、100 ms 节拍、最近 20 帧（2 秒）为一个样本。
划分以**整段音频**为单位（clip-level split），同一段音频的相邻窗口不会同时出现在训练和
验证里——窗口级随机划分会高估准确率。

用法：
    python scripts/make_dataset.py --src data/synth/train --out artifacts/dataset_synth.npz
    python scripts/make_dataset.py --src data/real/20261015 --out artifacts/dataset_real.npz --no-split
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import (WINDOW_FRAMES, FEATURE_NAMES, analyze_frames,  # noqa: E402
                      load_wav_mono, window_vector)

WINDOW_HOP_FRAMES = 5          # 0.5 s 一个窗口


def random_when(rng: np.random.Generator) -> datetime:
    """训练用的时间先验：清醒时段为主，周末两天占 2/7。与类别无关（合成语料不注入时间相关性）。"""
    hour = int(np.clip(rng.normal(14.0, 4.0), 6, 23))
    day = int(rng.integers(0, 7))
    if rng.random() < 0.7:      # 工作日
        day = int(rng.integers(0, 5))
    return datetime(2026, 10, 5 + day if day < 5 else 10, hour, int(rng.integers(0, 60)))


def extract_clip(path: Path, rng: np.random.Generator):
    samples = load_wav_mono(path)
    frames = analyze_frames(samples)
    if frames is None:
        return []
    n_frames = len(frames["t_s"])
    when = random_when(rng)
    rows = []
    for start in range(0, max(n_frames - WINDOW_FRAMES + 1, 1), WINDOW_HOP_FRAMES):
        vec = window_vector(frames, start, start + WINDOW_FRAMES, motion=None, when=when)
        rows.append(vec)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="<src>/<class>/*.wav 目录结构")
    ap.add_argument("--out", required=True)
    ap.add_argument("--val-every", type=int, default=5,
                    help="每 N 段抽 1 段作验证（clip-level）")
    ap.add_argument("--no-split", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    src = Path(args.src)
    classes = sorted([d.name for d in src.iterdir() if d.is_dir()])
    if not classes:
        raise SystemExit(f"没有类别目录: {src}")
    class_to_idx = {c: i for i, c in enumerate(classes)}

    X_tr, y_tr, meta_tr = [], [], []
    X_va, y_va, meta_va = [], [], []
    for cls in classes:
        wavs = sorted((src / cls).glob("*.wav"))
        for k, wav in enumerate(wavs):
            rng = np.random.default_rng(args.seed + 131 * k + class_to_idx[cls])
            rows = extract_clip(wav, rng)
            is_val = (not args.no_split) and (k % args.val_every == 0)
            for r in rows:
                (X_va if is_val else X_tr).append(r)
                (y_va if is_val else y_tr).append(class_to_idx[cls])
                (meta_va if is_val else meta_tr).append(f"{cls}/{wav.name}")
        print(f"{cls}: {len(wavs)} clips")

    X_tr = np.asarray(X_tr, dtype=np.float32)
    y_tr = np.asarray(y_tr, dtype=np.int64)
    X_va = np.asarray(X_va, dtype=np.float32)
    y_va = np.asarray(y_va, dtype=np.int64)
    if len(X_tr) == 0:
        raise SystemExit("训练集为空")

    mu = X_tr.mean(axis=0)
    sigma = X_tr.std(axis=0)
    sigma[sigma < 1e-6] = 1.0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        X_train=X_tr, y_train=y_tr,
        X_val=X_va, y_val=y_va,
        classes=np.array(classes),
        feature_names=np.array(FEATURE_NAMES),
        mu=mu, sigma=sigma,
        meta_train=np.array(meta_tr), meta_val=np.array(meta_va),
    )
    summary = {
        "src": str(src), "out": str(out), "classes": classes,
        "train_samples": int(len(X_tr)), "val_samples": int(len(X_va)),
        "feature_count": int(X_tr.shape[1]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
