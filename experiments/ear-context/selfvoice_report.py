#!/usr/bin/env python3
"""轴2（我是否在说话）从纯 IMU 能不能分出来 —— 可分性报告。

输入：collect_selfvoice.py 采出来的会话目录（samples.jsonl + labels.jsonl [+ audio.wav]）。

这份报告只回答一个问题，而且只回答这一个：
    **在"我在说话"和"我没说话但在动（抖腿/咀嚼/点头）"之间，六轴里有没有可分的信息？**

它不训练模型、不导出权重、不碰固件。分不开就该停，别在模型结构上浪费时间。

评估方式：留一段法（leave-one-segment-out），**把各折的预测汇总后算一个 AUC**。
逐折算 AUC 是错的 —— 留出的那一段本身只有一个标签，AUC 没有定义（第一版就踩了这个坑）。
所以每一折只产出一段预测，最后把所有折拼起来算总 AUC，这样每个窗口的预测都来自一个
没见过它所在段的模型。

同一段里的窗口高度相关，随机拆窗口会把分数抬高一大截 —— 这个项目已经吃过一次亏
（make_dataset.py 用 clip-level split 就是为这个）。

用法：
    python3 selfvoice_report.py --session ../listening-model/data/real/20261015T...-selfvoice
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

WINDOW_S = 0.5          # 每个特征窗口
HOP_S = 0.25
SPEECH_BAND = (1.0, 4.0)    # 下颌/头部随语音运动的基频带
HIGH_BAND = (4.0, 8.0)      # 辅音与咀嚼常见的高一档

# 「我在说话」的两段都算正类。speech_music 同时有音乐，单列出来看 —— 它是最难的一档。
POSITIVE_LABELS = ("self_speech", "speech_music")
NEGATIVE_LABELS = ("silent", "fidget", "chew_nod")


# ── 读数据 ──────────────────────────────────────────────────────────────────

def load_imu(session: Path):
    t_mono, accel, gyro = [], [], []
    with (session / "samples.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") != "imu":
                continue
            a, g = r.get("accel_mps2"), r.get("gyro_dps")
            if not (isinstance(a, list) and isinstance(g, list)):
                continue
            t_mono.append(r["host_mono_ns"])
            accel.append(a)
            gyro.append(g)
    if not t_mono:
        raise SystemExit("samples.jsonl 里没有 imu 记录")
    return (np.asarray(t_mono, dtype=np.float64) / 1e9,
            np.asarray(accel, dtype=np.float64),
            np.asarray(gyro, dtype=np.float64))


def load_labels(session: Path):
    rows = []
    with (session / "labels.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            rows.append((r["label"], r["start_mono_ns"] / 1e9, r["end_mono_ns"] / 1e9))
    return rows


# ── 特征 ────────────────────────────────────────────────────────────────────
#
# 只用**相对运动模式**，不用任何绝对朝向。
# 理由：这个设备的 pitch/roll 在静止时会自己漂（已知未修，见 ear-context README），
# 任何依赖绝对姿态的特征都会被那个漂移污染。能量、频谱占比、一阶差分都不受影响。

def band_energy(x: np.ndarray, fs: float, lo: float, hi: float) -> float:
    if x.size < 8 or fs <= 0:
        return 0.0
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / fs)
    m = (freqs >= lo) & (freqs < hi)
    total = spec.sum()
    return float(spec[m].sum() / total) if total > 1e-12 else 0.0


def window_features(a: np.ndarray, g: np.ndarray, fs: float) -> dict[str, float]:
    """a: (n,3) 加速度 m/s²（含重力）；g: (n,3) 角速度 °/s。"""
    amag = np.linalg.norm(a, axis=1)
    gmag = np.linalg.norm(g, axis=1)
    # 去重力：窗口只有 0.5 s，姿态变化远慢于这个尺度，减均值就够
    a_dyn = amag - amag.mean()
    g_dyn = gmag - gmag.mean()
    jerk = np.diff(amag) * fs if amag.size > 1 else np.zeros(1)
    return {
        "acc_rms": float(np.sqrt(np.mean(a_dyn ** 2))),
        "acc_std": float(amag.std()),
        "acc_band_speech": band_energy(amag, fs, *SPEECH_BAND),
        "acc_band_high": band_energy(amag, fs, *HIGH_BAND),
        "jerk_rms": float(np.sqrt(np.mean(jerk ** 2))),
        "gyro_rms": float(np.sqrt(np.mean(g_dyn ** 2))),
        "gyro_std": float(gmag.std()),
        "gyro_band_speech": band_energy(gmag, fs, *SPEECH_BAND),
        "gyro_band_high": band_energy(gmag, fs, *HIGH_BAND),
        "acc_axis_std_max": float(a.std(axis=0).max()),
        "gyro_axis_std_max": float(g.std(axis=0).max()),
    }


def extract(session: Path):
    t, accel, gyro = load_imu(session)
    labels = load_labels(session)
    fs = 1.0 / np.median(np.diff(t))
    rows = []
    for label, s, e in labels:
        idx = np.where((t >= s) & (t < e))[0]
        if idx.size < 20:
            continue
        w = int(WINDOW_S * fs)
        hop = max(1, int(HOP_S * fs))
        for start in range(0, idx.size - w + 1, hop):
            sel = idx[start:start + w]
            if np.any(np.diff(sel) > 3):     # 跨丢包的窗口直接跳过
                continue
            f = window_features(accel[sel], gyro[sel], fs)
            f["_label"] = label
            rows.append(f)
    return rows, fs, t


# ── 评估 ────────────────────────────────────────────────────────────────────

def auc(score: np.ndarray, y: np.ndarray) -> float:
    """秩和法 AUC；y=1 为正类。单类别输入返回 nan。"""
    pos, neg = score[y == 1], score[y == 0]
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    both = np.concatenate([pos, neg])
    order = np.argsort(both, kind="mergesort")
    ranks = np.empty(both.size)
    ranks[order] = np.arange(1, both.size + 1)
    r = ranks[:pos.size].sum()
    return float((r - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size))


def fit_logreg(X: np.ndarray, y: np.ndarray, iters: int = 500, lr: float = 0.5,
               l2: float = 1e-2):
    """够用就好的逻辑回归；这个脚本不想为此引入 sklearn。"""
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xs = np.hstack([(X - mu) / sd, np.ones((X.shape[0], 1))])
    w = np.zeros(Xs.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Xs @ w, -30, 30)))
        grad = Xs.T @ (p - y) / len(y) + l2 * np.concatenate([w[:-1], [0.0]])
        w -= lr * grad
    return w, mu, sd


def predict(w, mu, sd, X):
    Xs = np.hstack([(X - mu) / sd, np.ones((X.shape[0], 1))])
    return 1.0 / (1.0 + np.exp(-np.clip(Xs @ w, -30, 30)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--exclude", nargs="*", default=None,
                    help="剔除某些段（例如核查发现没照做的 speech_music）")
    args = ap.parse_args()
    s = args.session

    rows, fs, t = extract(s)
    if not rows:
        raise SystemExit("没有可用的窗口；检查 labels.jsonl 和采样率")
    if args.exclude:
        keep = [r for r in rows if r["_label"] not in set(args.exclude)]
        dropped = len(rows) - len(keep)
        print(f"按 --exclude 剔除 {dropped} 个窗口：{', '.join(args.exclude)}")
        print("（只该在合规性核查确认那一段没照做之后才用；"
              "错误标签会让分类器整体反向。）\n")
        rows = keep
        if not rows:
            raise SystemExit("剔完没有窗口了")
    feats = [k for k in rows[0] if not k.startswith("_")]
    X = np.array([[r[f] for f in feats] for r in rows])
    labels = np.array([r["_label"] for r in rows])
    usable = np.isin(labels, POSITIVE_LABELS + NEGATIVE_LABELS)
    y = np.isin(labels, POSITIVE_LABELS).astype(int)

    print(f"会话：{s}")
    print(f"六轴 {fs:.1f} Hz，时长 {t[-1] - t[0]:.1f} s，窗口 {len(rows)} 个"
          f"（{WINDOW_S}s / hop {HOP_S}s）")
    print(f"正类段 {POSITIVE_LABELS}，负类段 {NEGATIVE_LABELS}\n")

    # ── 1. 逐段特征中位数：先肉眼看有没有差异 ──
    print("各段特征中位数")
    print("  " + "label".ljust(14) + "".join(f"{f[:12]:>13}" for f in feats))
    for lab in POSITIVE_LABELS + NEGATIVE_LABELS:
        m = labels == lab
        if not m.any():
            continue
        print("  " + lab.ljust(14)
              + "".join(f"{np.median(X[m, i]):13.4f}" for i in range(len(feats))))

    # ── 2. 单特征 AUC ──
    print("\n单特征 AUC（正类 = 我在说话）")
    print("  " + "feature".ljust(20) + "".join(f"{c:>12}" for c in NEGATIVE_LABELS)
          + f"{'ALL_NEG':>12}")
    for i, f in enumerate(feats):
        cells = []
        for neg in NEGATIVE_LABELS:
            m = np.isin(labels, (neg, *POSITIVE_LABELS))
            cells.append(auc(X[m, i], y[m]))
        cells.append(auc(X[usable, i], y[usable]))
        print("  " + f.ljust(20)
              + "".join(f"{a:12.3f}" if np.isfinite(a) else f"{'—':>12}" for a in cells))

    # ── 3. 留一段法：每个窗口的预测都来自没见过它所在段的模型 ──
    print("\n留一段法（leave-one-segment-out）")
    pred = np.full(len(labels), np.nan)
    for lab in sorted(set(labels[usable])):
        hold = labels == lab
        train = usable & ~hold
        if train.sum() < 20 or len(set(y[train])) < 2 or hold.sum() < 5:
            continue
        w, mu, sd = fit_logreg(X[train], y[train].astype(float))
        pred[hold] = predict(w, mu, sd, X[hold])
    ok = np.isfinite(pred) & usable

    print("\n  每段的预测概率（最该看的一张表）")
    print("    segment          窗数   中位 p   均值 p")
    for lab in POSITIVE_LABELS + NEGATIVE_LABELS:
        m = ok & (labels == lab)
        if not m.any():
            continue
        mark = "  ← 正类" if lab in POSITIVE_LABELS else ""
        print(f"    {lab:<16}{m.sum():4d}   {np.median(pred[m]):7.3f}  "
              f"{pred[m].mean():7.3f}{mark}")

    if ok.sum() == 0 or len(set(y[ok])) < 2:
        print("\n  段数不够，没法做留一段法：每一折都得有正类段和负类段可供训练。")
        print("  至少要按完整流程采一遍（silent / self_speech / fidget / chew_nod）。")
        print("\n  判定：样本不足，无法判断")
        return 1

    overall = auc(pred[ok], y[ok])
    print(f"\n  汇总 AUC = {overall:.3f}"
          f"   （所有折的预测拼起来算一次；逐折算 AUC 没有定义）")
    for neg in NEGATIVE_LABELS:
        m = ok & np.isin(labels, (neg, *POSITIVE_LABELS))
        a = auc(pred[m], y[m])
        if np.isfinite(a):
            print(f"    我在说话 vs {neg:<10} AUC = {a:.3f}")
    acc = ((pred[ok] >= 0.5).astype(int) == y[ok]).mean()
    print(f"  阈值 0.5 的准确率 = {acc:.3f}（{ok.sum()} 个窗口）")

    verdict = ("有可分信息，值得继续" if overall >= 0.85 else
               "有信号但不够干净，先看混淆在哪" if overall >= 0.70 else
               "基本不可分，建议停")
    print(f"\n  判定：{verdict}")

    # ── 4. 录音合规性自检 ──
    audio_path = s / "audio.wav"
    audio_ok = False
    if audio_path.exists():
        try:
            with wave.open(str(audio_path)) as w:
                n, sr = w.getnframes(), w.getframerate()
                pcm = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64)
            env = np.abs(pcm) / 32768.0
            print(f"\n录音自检：{n / sr:.1f} s @ {sr} Hz，"
                  f"整体 RMS 中位 {np.median(env):.5f}，p95 {np.percentile(env, 95):.5f}")
            print("  只用来确认你照做了：self_speech 段应明显比 silent 段响。")
            audio_ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"\n录音自检跳过：{exc}")
    else:
        print("\n没有 audio.wav —— 无法独立验证你是否照做了，结论要打折看。")

    out = s / "selfvoice-report.json"
    out.write_text(json.dumps({
        "fs_hz": fs, "windows": int(ok.sum()), "features": feats,
        "loso_auc": overall if np.isfinite(overall) else None,
        "accuracy_at_0.5": float(acc),
        "verdict": verdict,
        "audio_present": audio_ok,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
