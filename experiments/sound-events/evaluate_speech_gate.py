"""在一个"阶段标签已知"的真机采集上评估说话 gate（按阶段统计召回/误报）。

阶段命名约定（tools/collect_teacher_data.py --script）：
  * `speech*` 段 = 正样本；
  * `quiet*` / `wind*` / `keyboard*` 段 = 负样本；
  * 其它段忽略。
阶段边界用教师 YAMNet 首次 speech>0.5 与 `phases.json` 首段对齐（与训练脚本一致）。

用法：
    .venv/bin/python experiments/sound-events/evaluate_speech_gate.py \
        记录/教师数据/20261019-163120-gateeval
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments" / "listening-model" / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "export_speech_gate", ROOT / "experiments" / "sound-events" / "export_speech_gate.py")
assert SPEC and SPEC.loader
EXPORT = importlib.util.module_from_spec(SPEC)
sys.modules["export_speech_gate"] = EXPORT
SPEC.loader.exec_module(EXPORT)

MASKED = np.ones(92, dtype=bool)
MASKED[list(range(72, 80)) + [84]] = False


def session_windows(directory: Path):
    frames = [json.loads(line) for line in
              (directory / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line]
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    phases = json.loads((directory / "phases.json").read_text(encoding="utf-8"))["phases"]
    first_chunk = meta["first_chunk_ts_ms"]
    frame_time = np.array([(frame["t_ms"] - first_chunk) / 1000.0 for frame in frames])

    teacher = [json.loads(line) for line in
               (directory / "teacher_yamnet.jsonl").read_text(encoding="utf-8").splitlines()
               if line]
    teacher_time = np.array([row["center_s"] for row in teacher])
    speech = np.array([row["targets"].get("speech", 0.0) for row in teacher])
    candidates = np.where(speech > 0.5)[0]
    offset = (float(teacher_time[candidates[0]] - phases[0]["host_ms"] / 1000.0)
              if len(candidates) else 0.3)

    intervals = []
    for index, phase in enumerate(phases):
        start = phase["host_ms"] / 1000.0 - offset
        end = (phases[index + 1]["host_ms"] / 1000.0 - offset
               if index + 1 < len(phases) else 1e9)
        intervals.append((phase["phase"], start, end))

    bands = np.array([frame["bands"] for frame in frames], dtype=np.float64)
    windows = EXPORT.windows_from_bands(bands)
    windows[:, ~MASKED] = 0.0
    # 窗末帧时间（导出脚本的窗是 20 帧、hop 5 帧）
    window_time = np.array([frame_time[min(len(frame_time) - 1, start + 19)]
                            for start in range(0, len(frames) - 19, 5)])[:len(windows)]
    return windows, window_time, intervals


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    directory = Path(sys.argv[1])

    multi = EXPORT.teacher_session(EXPORT.MULTI_DIR)
    tts = EXPORT.teacher_session(EXPORT.TTS_DIR)
    human = EXPORT.human_session(EXPORT.B_CSV)
    train_x = np.concatenate([multi[0], tts[0], human[0]])
    train_y = np.concatenate([multi[1], tts[1], human[1]])
    train_x[:, ~MASKED] = 0.0
    w1, b1, w2, b2 = EXPORT.train(train_x, train_y)

    windows, window_time, intervals = session_windows(directory)
    probability = np.array([EXPORT.infer(x, w1, b1, w2, b2) for x in windows])
    predicted = probability > 0.5

    totals = {"speech": [0, 0], "negative": [0, 0]}  # [predicted, count]
    print(f"{directory.name}: {len(windows)} 窗")
    for name, start, end in intervals:
        mask = (window_time >= start) & (window_time < end)
        if not mask.any():
            continue
        kind = ("speech" if name.startswith("speech")
                else "negative" if name.startswith(("quiet", "wind", "keyboard")) else None)
        if kind is None:
            continue
        totals[kind][0] += int(predicted[mask].sum())
        totals[kind][1] += int(mask.sum())
        print(f"  {name:12s} n={int(mask.sum()):3d} 判 speech "
              f"{100.0 * predicted[mask].mean():3.0f}% mean p {probability[mask].mean():.3f}")

    speech_hits, speech_total = totals["speech"]
    negative_hits, negative_total = totals["negative"]
    if speech_total:
        print(f"speech 召回 {speech_hits}/{speech_total} = {speech_hits / speech_total:.2f}")
    if negative_total:
        print(f"非语音误报 {negative_hits}/{negative_total} = "
              f"{negative_hits / negative_total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
