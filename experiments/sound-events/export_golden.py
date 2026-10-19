"""从真机采集导出 C++ 单测金标准：`test/sound_event_golden.h`。

每个样本包含：

* 窗内 10 帧的 24 段 dBFS 与宽带电平（float，够单测比对）；
* 该窗时刻的噪声地板 `base` / `base_level`（由同一份 `FloorTracker` 回放得到）；
* 训练侧算出的 15 维特征（`features.py::window_features`，权威口径）；
* 训练侧规则的判断结果（`evaluate.py::raw_classify`）。

C++ 单测做三件事：复算 15 维特征 → 与金标准比对；用默认阈值判类 → 与金标准比对；
噪声地板/状态机/撞击清窗用合成序列单测。这样"口径错位"会在主机上直接报错，而不是
到设备上才变成"模型安静地变差"。

用法：
    python3 experiments/sound-events/export_golden.py \
        --out test/sound_event_golden.h \
        记录/键盘风声/20261019-141545/live.csv \
        记录/键盘风声/20261019-142748-speech/live.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate import Rule, raw_classify  # noqa: E402
from features import (FEATURE_NAMES, PHASE_TO_CLASS, WINDOW_FRAMES,  # noqa: E402
                      FloorTracker, Frame, load_capture, window_features)


def build_samples(paths: list[Path], per_class: int) -> list[dict]:
    """先收集所有候选窗，再按"真值类别"均衡挑选。

    挑选时优先选规则判对的窗（保证金标准里有 keyboard/wind 的正样本），不足再补判错的窗；
    每个 5 s block 最多取一个，避免相邻窗高度相关。
    """
    candidates: list[dict] = []
    for path in paths:
        frames = load_capture(path)
        tracker = FloorTracker()
        ring: list[Frame] = []
        previous_phase = frames[0][1]
        phase_start = frames[0][2]
        last_block = -1
        for frame, phase, t in frames:
            if phase != previous_phase:
                previous_phase, phase_start = phase, t
            tracker.step(frame.band_dbfs, frame.broadband_dbfs, frame.mic_valid)
            ring.append(frame)
            if len(ring) > WINDOW_FRAMES:
                ring.pop(0)
            if not tracker.ready or len(ring) < WINDOW_FRAMES:
                continue
            if t - phase_start < 2.5 or t < 20.0:
                continue
            if not all(item.mic_valid for item in ring):
                continue
            block = int(t // 5)
            if block == last_block:
                continue
            label = PHASE_TO_CLASS.get(phase, phase)
            features = window_features(tracker.base, tracker.level, ring)
            candidates.append({
                "truth": label,
                "label": f"{path.parent.name}:{phase}:{t:.1f}s",
                "base": tracker.base.copy(),
                "base_level": tracker.level,
                "bands": np.stack([item.band_dbfs for item in ring]),
                "broadband": np.array([item.broadband_dbfs for item in ring]),
                "features": np.array([features[name] for name in FEATURE_NAMES]),
                "expected_class": raw_classify(features, Rule()),
                "block": block,
            })
            last_block = block

    selected: list[dict] = []
    for label in ("quiet", "speech", "keyboard", "wind"):
        pool = [item for item in candidates if item["truth"] == label]
        exact = [item for item in candidates if item["truth"] == label
                 and item["expected_class"] == label]
        others = [item for item in pool if item["expected_class"] != label]
        # 每隔几个取一个，覆盖不同时段；exact 优先。
        ordered = exact + others
        for index, item in enumerate(ordered):
            if len([x for x in selected if x["truth"] == label]) >= per_class:
                break
            if any(abs(item["block"] - picked["block"]) < 1 and item["label"].split(":")[0]
                   == picked["label"].split(":")[0]
                   for picked in selected if picked["truth"] == label):
                continue
            selected.append(item)
    # 第二遍：保证"规则判断"的每一类也有样本（真值均衡只保证输入分布，不保证判类分支被覆盖）。
    for target in ("quiet", "speech", "keyboard", "wind"):
        have = sum(1 for item in selected if item["expected_class"] == target)
        if have >= 3:
            continue
        for item in candidates:
            if item["expected_class"] != target or item in selected:
                continue
            if any(item["block"] == picked["block"] and item["label"].split(":")[0]
                   == picked["label"].split(":")[0] for picked in selected):
                continue
            selected.append(item)
            have += 1
            if have >= 3:
                break
    return selected


def format_float(value: float) -> str:
    return f"{value:.4f}f"


def emit(samples: list[dict], out_path: Path) -> None:
    lines = [
        "// 由 experiments/sound-events/export_golden.py 生成，不要手改。",
        "// 数据来源：记录/键盘风声/ 下两段真机采集（CoreS3 ES7210，24 段 10 Hz）。",
        "// 用途：C++ 端的 15 维事件特征与规则判断必须与训练侧逐值一致。",
        "#pragma once",
        "",
        "#include <array>",
        "",
        "namespace sound_event_golden {",
        "",
        f"constexpr std::size_t kWindowFrames = {WINDOW_FRAMES};",
        "constexpr std::size_t kBandCount = 24;",
        "constexpr std::size_t kFeatureCount = 15;",
        "",
        "struct Sample {",
        "    const char* label;",
        "    std::array<float, kBandCount> base;",
        "    float base_level;",
        "    std::array<std::array<float, kBandCount>, kWindowFrames> bands;",
        "    std::array<float, kWindowFrames> broadband;",
        "    std::array<float, kFeatureCount> expected;",
        "    const char* expected_class;",
        "};",
        "",
        "inline constexpr Sample kSamples[] = {",
    ]
    for sample in samples:
        lines.append(f"    // {sample['label']}  -> {sample['expected_class']}")
        lines.append("    {")
        lines.append(f'        "{sample["label"]}",')
        base = ", ".join(format_float(v) for v in np.asarray(sample["base"]))
        lines.append(f"        {{{base}}},")
        lines.append(f"        {format_float(float(sample['base_level']))},")
        lines.append("        {{")
        for row in np.asarray(sample["bands"]):
            lines.append("            {" + ", ".join(format_float(v) for v in row) + "},")
        lines.append("        }},")
        lines.append("        {" + ", ".join(format_float(v) for v in np.asarray(sample["broadband"])) + "},")
        lines.append("        {" + ", ".join(format_float(v) for v in np.asarray(sample["features"])) + "},")
        lines.append(f'        "{sample["expected_class"]}",')
        lines.append("    },")
    lines += [
        "};",
        "",
        "}  // namespace sound_event_golden",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(samples)} 个样本 -> {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--out", default=Path("test/sound_event_golden.h"), type=Path)
    parser.add_argument("--per-class", type=int, default=6)
    args = parser.parse_args()
    samples = build_samples(args.captures, args.per_class)
    emit(samples, args.out)
    truth: dict[str, int] = {}
    predicted: dict[str, int] = {}
    for sample in samples:
        truth[sample["truth"]] = truth.get(sample["truth"], 0) + 1
        predicted[sample["expected_class"]] = predicted.get(sample["expected_class"], 0) + 1
    print("真值分布：", truth)
    print("规则判断分布：", predicted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
