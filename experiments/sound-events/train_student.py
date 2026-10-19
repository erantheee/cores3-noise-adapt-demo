"""训练 15→16→4 的端侧事件学生模型（教师-学生蒸馏的第一版学生）。

输入两类数据：

1. **带人工分段标签的采集**（`tools/live_band_capture.py` 的 CSV）：直接给 4 类标签；
2. **同时采了音频+频段的教师会话**（`tools/collect_teacher_data.py --script` 的目录）：
   用阶段边界对齐到设备帧，给 4 类标签。音频侧还可以用
   `teacher_yamnet_official.py` / `teacher_ast.py` 做校验（本脚本只用阶段标签）。

学生结构固定为 15 → 16 → 4（324 参数，<2 KB，推理微秒级），与 `main/sound_event.hpp`
的 15 维特征同口径；训练后可与规则逐窗对比。**不自动上机**：见 D18 准入线，跨条件误报
必须不升才允许替换规则。

用法：
    .venv/bin/python experiments/sound-events/train_student.py \
        --train 记录/键盘风声/20261019-141545/live.csv \
                记录/键盘风声/20261019-142748-speech/live.csv \
        --teacher-session 记录/教师数据/20261019-151706-multi \
        --holdout 记录/键盘风声/20261019-144909-v5/live.csv \
                  记录/键盘风声/20261019-145305-v5b/live.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import features as F  # noqa: E402

CLASSES = ["quiet", "speech", "keyboard", "wind"]
PHASE_MAP = {"ready": "quiet", "quiet": "quiet", "pause": "quiet", "done": "quiet",
             "keyboard": "keyboard", "wind": "wind", "speech": "speech", "knock": "quiet"}
KNOCK_MAP = {"knock": "quiet"}


def load_band_captures(paths: list[Path], hop: int = 2):
    if not paths:
        return np.zeros((0, F.FEATURE_NAMES.__len__())), np.array([], dtype=object), []
    dataset = F.build_dataset(paths, hop=hop)
    labels = np.array([KNOCK_MAP.get(value, value) for value in dataset["y"]])
    return dataset["X"], labels, dataset["features"]


def load_teacher_session(directory: Path, guard_s: float = 2.5):
    """从同时采集的目录里取 10 Hz 频段 + 阶段标签，回放出 15 维窗特征。"""
    frames = [json.loads(line) for line in
              (directory / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line]
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    phases = json.loads((directory / "phases.json").read_text(encoding="utf-8"))["phases"]
    if not frames or not phases:
        return np.zeros((0, len(F.FEATURE_NAMES))), np.array([], dtype=object)

    # 对齐：采集器在 phases.json 里记的是相对 start 的 host 毫秒；音频从第一个
    # mic_chunk 开始。教师标签（YAMNet 0.48 s 窗）能指出第一次人声提示语的位置，
    # 用它估计"host 时间 → 音频时间"的偏移；误差只影响段首 guard，不影响段中标签。
    offset = 0.0
    teacher_path = directory / "teacher_yamnet.jsonl"
    if teacher_path.exists():
        teacher = [json.loads(line) for line in
                   teacher_path.read_text(encoding="utf-8").splitlines() if line]
        spikes = [row["center_s"] for row in teacher
                  if row["targets"].get("speech", 0.0) > 0.5]
        if spikes:
            offset = spikes[0] - phases[0]["host_ms"] / 1000.0

    intervals = []
    for index, phase in enumerate(phases):
        start = phase["host_ms"] / 1000.0 - offset
        end = (phases[index + 1]["host_ms"] / 1000.0 - offset
               if index + 1 < len(phases) else 1e9)
        intervals.append((phase["phase"], start, end))

    first_chunk = meta["first_chunk_ts_ms"]
    tracker = F.FloorTracker()
    ring: list[F.Frame] = []
    samples: list[tuple[list[float], str]] = []
    for row in frames:
        band = np.array(row["bands"], dtype=np.float64)
        power = np.power(10.0, band / 10.0)
        broadband = 10.0 * np.log10(max(float(power.sum()), 1e-12))
        mic_valid = not (row.get("event") == "artifact" or row.get("context_valid") is False)
        tracker.step(band, broadband, mic_valid)
        ring.append(F.Frame(band, broadband, mic_valid))
        if len(ring) > F.WINDOW_FRAMES:
            ring.pop(0)
        if len(ring) < F.WINDOW_FRAMES or not tracker.ready:
            continue
        if not all(frame.mic_valid for frame in ring):
            continue
        audio_time = (row["t_ms"] - first_chunk) / 1000.0
        match = next(((name, start) for name, start, end in intervals
                      if start <= audio_time < end), None)
        if match is None:
            continue
        name, start = match
        if audio_time - start < guard_s:
            continue
        window = F.window_features(tracker.base, tracker.level, ring)
        samples.append(([window[key] for key in F.FEATURE_NAMES],
                        PHASE_MAP.get(name, name)))
    if not samples:
        return np.zeros((0, len(F.FEATURE_NAMES))), np.array([], dtype=object)
    return (np.array([sample[0] for sample in samples]),
            np.array([sample[1] for sample in samples]))


def rule_classify(window: dict) -> str:
    if (window["low_share"] < 0.55 and window["std_mid"] > 4.0
            and window["cent"] > 600.0
            and (window["slope"] > -1.35 or window["cent"] > 800.0)):
        return "keyboard"
    if (window["slope"] < -1.45 and window["vocal_peak"] < 0.45
            and window["d_lo"] > 16.0 and window["range_lo"] > 14.0):
        return "wind"
    if window["vocal_peak"] > 0.45:
        return "speech"
    return "quiet"


def train(X: np.ndarray, y: np.ndarray, epochs: int = 800):
    import torch
    import torch.nn as nn

    mean = X.mean(axis=0)
    std = np.maximum(X.std(axis=0), 1e-6)
    labels = np.array([CLASSES.index(value) for value in y])
    counts = np.bincount(labels, minlength=len(CLASSES))
    weights = len(labels) / (len(CLASSES) * np.maximum(counts, 1))
    model = nn.Sequential(nn.Linear(X.shape[1], 16), nn.ReLU(), nn.Linear(16, len(CLASSES)))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=1e-3)
    loss_fn = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32))
    inputs = torch.tensor((X - mean) / std, dtype=torch.float32)
    targets = torch.tensor(labels)
    for _ in range(epochs):
        optimizer.zero_grad()
        loss_fn(model(inputs), targets).backward()
        optimizer.step()
    model.eval()
    return model, mean, std


def predict(model, mean, std, X: np.ndarray) -> np.ndarray:
    import torch
    with torch.no_grad():
        logits = model(torch.tensor((X - mean) / std, dtype=torch.float32))
    return np.array(CLASSES)[logits.argmax(dim=1).numpy()]


def metrics(truth: np.ndarray, predicted: np.ndarray) -> dict:
    table = {want: {got: 0 for got in CLASSES} for want in CLASSES}
    for want, got in zip(truth, predicted):
        table[want][got] += 1
    non_event = sum(sum(table[c].values()) for c in ("quiet", "speech"))
    false_events = sum(table[c][e] for c in ("quiet", "speech")
                       for e in ("keyboard", "wind"))
    event_total = sum(sum(table[c].values()) for c in ("keyboard", "wind"))
    event_hit = sum(table[c][c] for c in ("keyboard", "wind"))
    return {
        "event_recall": event_hit / max(event_total, 1),
        "false_event_rate": false_events / max(non_event, 1),
        "accuracy": float(np.mean(truth == predicted)) if len(truth) else 0.0,
        "confusion": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="训练 15→16→4 事件学生模型")
    parser.add_argument("--train", nargs="*", type=Path, default=[])
    parser.add_argument("--teacher-session", nargs="*", type=Path, default=[])
    parser.add_argument("--holdout", nargs="*", type=Path, default=[])
    parser.add_argument("--epochs", type=int, default=800)
    parser.add_argument("--export-pt", type=Path, default=None)
    args = parser.parse_args()

    X_parts, y_parts = [], []
    X, y, _ = load_band_captures(args.train)
    X_parts.append(X); y_parts.append(y)
    for directory in args.teacher_session:
        X, y = load_teacher_session(directory)
        print(f"教师会话 {directory.name}: {len(y)} 窗 {dict(Counter(y))}")
        X_parts.append(X); y_parts.append(y)
    X_train = np.concatenate(X_parts)
    y_train = np.concatenate(y_parts)
    if len(y_train) == 0:
        raise SystemExit("没有训练样本")
    print(f"训练集 {len(y_train)} 窗：{dict(Counter(y_train))}")
    model, mean, std = train(X_train, y_train, args.epochs)
    if args.export_pt:
        import torch
        torch.save({"state_dict": model.state_dict(), "mean": mean, "std": std,
                    "classes": CLASSES, "features": F.FEATURE_NAMES}, args.export_pt)
        print(f"模型 -> {args.export_pt}")

    for holdout in args.holdout:
        X_test, y_test, feature_dicts = load_band_captures([holdout])
        if len(y_test) == 0:
            continue
        student = predict(model, mean, std, X_test)
        rule = np.array([rule_classify(window) for window in feature_dicts])
        for name, predicted in (("学生", student), ("规则", rule)):
            result = metrics(y_test, predicted)
            print(f"\n{holdout.parent.name} / {name}: n={len(y_test)} "
                  f"recall={result['event_recall']:.2f} "
                  f"false_events={result['false_event_rate']:.2f} "
                  f"acc={result['accuracy']:.2f}")
            for want in CLASSES:
                print(f"  {want:9s}{result['confusion'][want]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
