"""在真机采集上评估键盘/风声事件规则（离线原型）。

指标口径（比"逐窗准确率"更贴近用途）：

* **事件类召回**：keyboard / wind 窗口被正确判成该类的比例；
* **非事件类误报**：quiet / speech 窗口被判成 keyboard 或 wind 的比例（这是产品最痛的方向——
  键盘检测误把说话当键盘，会重复"说话时误下压"的老问题）；
* **留出验证**：每个采集按时间前 60% / 后 40% 切开，前者参与调参、后者只报告。规则阈值是
  在这两个采集上调出来的，所以**留出数字仍然不是跨条件泛化证据**，只是防止"同一段自我复读"。

规则结构（全部消费 24 段 dBFS，可在 C++ 固件里逐行照搬）：

    keyboard  low_share < 0.55 且 std_mid > 4 dB 且 cent > 600 Hz
              且（slope > -1.35 或 cent > 800 Hz）
    wind      slope < -1.45 且 vocal_peak < 0.45 且 d_lo > 16 dB 且 range_lo > 14 dB
    speech    vocal_peak > 0.45
    quiet     其余

判定后再过状态机：keyboard 保持 0.8 s、wind 保持 2.0 s、speech 保持 0.8 s，避免逐帧闪烁；
keyboard 的保持优先，不会被风覆盖。

用法：
    python3 experiments/sound-events/evaluate.py \
        记录/键盘风声/20261019-141545/live.csv \
        记录/键盘风声/20261019-142748-speech/live.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import CLASSES, FEATURE_NAMES, build_dataset  # noqa: E402


@dataclass(frozen=True)
class Rule:
    """规则阈值。固件 `sound_event.hpp` 的默认值必须与这里逐字段一致。"""

    keyboard_low_share: float = 0.55
    keyboard_std_mid: float = 4.0
    keyboard_cent: float = 600.0
    #: 键盘的频谱斜率不能太陡：阵风/低频轰鸣也有高 std_mid，但斜率 ≤ −1.45。
    #: 质心特别高（>800 Hz）时例外——强宽带咔哒本来就该过，不必再卡斜率。
    #: 现场复测（2026-10-19 v5 采集）：这条组合把"风被判成键盘"从 25% 降到 15%，
    #: 键盘召回不变（115/126）。
    keyboard_slope: float = -1.35
    keyboard_cent_escape: float = 800.0
    wind_slope: float = -1.45
    wind_vocal_peak: float = 0.45
    wind_d_lo: float = 16.0
    wind_range_lo: float = 14.0
    speech_vocal_peak: float = 0.45


@dataclass(frozen=True)
class Hold:
    """状态保持时间（秒）。keyboard 优先于 wind。"""

    keyboard_s: float = 0.8
    wind_s: float = 2.0
    speech_s: float = 0.8


def raw_classify(features: dict, rule: Rule) -> str:
    if (features["low_share"] < rule.keyboard_low_share
            and features["std_mid"] > rule.keyboard_std_mid
            and features["cent"] > rule.keyboard_cent
            and (features["slope"] > rule.keyboard_slope
                 or features["cent"] > rule.keyboard_cent_escape)):
        return "keyboard"
    if (features["slope"] < rule.wind_slope
            and features["vocal_peak"] < rule.wind_vocal_peak
            and features["d_lo"] > rule.wind_d_lo
            and features["range_lo"] > rule.wind_range_lo):
        return "wind"
    if features["vocal_peak"] > rule.speech_vocal_peak:
        return "speech"
    return "quiet"


def smooth(sequence: list[str], times: np.ndarray, sessions: np.ndarray,
           hold: Hold) -> np.ndarray:
    """逐窗状态机。时间必须按 (session, t) 排序后用，这里自己排序。"""
    result = np.empty(len(sequence), dtype=object)
    order = np.lexsort((times, sessions))
    state = "quiet"
    hold_until = -1.0
    previous_session = None
    for index in order:
        if sessions[index] != previous_session:
            state, hold_until = "quiet", -1.0
            previous_session = sessions[index]
        raw, now = sequence[index], times[index]
        if raw == "keyboard":
            state, hold_until = "keyboard", now + hold.keyboard_s
        elif raw == "wind" and not (state == "keyboard" and now <= hold_until):
            state, hold_until = "wind", now + hold.wind_s
        elif raw == "speech" and now > hold_until:
            state, hold_until = "speech", now + hold.speech_s
        elif raw == "quiet" and now > hold_until:
            state = "quiet"
        result[index] = state
    return result


def confusion(truth: np.ndarray, predicted: np.ndarray) -> dict:
    table = {want: {got: 0 for got in CLASSES} for want in CLASSES}
    for want, got in zip(truth, predicted):
        table[want][got] += 1
    return table


def metrics(table: dict) -> dict:
    event_total = sum(sum(table[c].values()) for c in ("keyboard", "wind"))
    event_hit = sum(table[c][c] for c in ("keyboard", "wind"))
    non_total = sum(sum(table[c].values()) for c in ("quiet", "speech"))
    false_events = sum(table[c][e] for c in ("quiet", "speech")
                       for e in ("keyboard", "wind"))
    return {
        "event_recall": event_hit / max(event_total, 1),
        "false_event_rate": false_events / max(non_total, 1),
        "keyboard_recall": table["keyboard"]["keyboard"] / max(sum(table["keyboard"].values()), 1),
        "wind_recall": table["wind"]["wind"] / max(sum(table["wind"].values()), 1),
        "speech_recall": table["speech"]["speech"] / max(sum(table["speech"].values()), 1),
        "quiet_recall": table["quiet"]["quiet"] / max(sum(table["quiet"].values()), 1),
    }


def print_table(table: dict, title: str) -> None:
    print(title)
    print(f"{'':<10}" + "".join(f"{c:>10}" for c in CLASSES) + "    n")
    for want in CLASSES:
        row = table[want]
        cells = "".join(f"{row[got]:>10}" for got in CLASSES)
        print(f"{want:<10}{cells}  {sum(row.values()):>5}")


def print_metrics(m: dict) -> None:
    print(f"  keyboard/wind 召回 {100 * m['event_recall']:5.1f}%"
          f"（keyboard {100 * m['keyboard_recall']:4.0f}% / wind {100 * m['wind_recall']:4.0f}%）")
    print(f"  安静/说话误报事件   {100 * m['false_event_rate']:5.1f}%")
    print(f"  speech 召回 {100 * m['speech_recall']:5.1f}%   quiet 召回 {100 * m['quiet_recall']:5.1f}%")


def segment_report(dataset: dict, predicted: np.ndarray) -> None:
    """按连续同类块报告检测率。wind 的阵风间隙会拉低逐窗数字，分块更能看出行为。"""
    y, times, sessions = dataset["y"], dataset["times"], dataset["sessions"]
    print("\n连续片段检测率（每段至少 8 窗）")
    for session in np.unique(sessions):
        index = np.where(sessions == session)[0]
        index = index[np.argsort(times[index])]
        block: list[int] = []
        previous = None
        for i in index:
            if previous is not None and y[i] != previous:
                if len(block) >= 8:
                    arr = np.array(block)
                    print(f"  session{session} {previous:9s} {times[arr[0]]:6.1f}-{times[arr[-1]]:6.1f}s "
                          f"n={len(arr):3d} 检测率 {100 * np.mean(predicted[arr] == y[arr]):3.0f}%")
                block = []
            previous = y[i]
            block.append(i)
        if len(block) >= 8:
            arr = np.array(block)
            print(f"  session{session} {previous:9s} {times[arr[0]]:6.1f}-{times[arr[-1]]:6.1f}s "
                  f"n={len(arr):3d} 检测率 {100 * np.mean(predicted[arr] == y[arr]):3.0f}%")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--print-features", action="store_true")
    args = parser.parse_args()

    rule, hold = Rule(), Hold()
    dataset = build_dataset(args.captures)
    print(f"样本 {len(dataset['y'])}，类别 {dict(zip(*np.unique(dataset['y'], return_counts=True)))}")
    print(f"撞击/无效窗 {dataset['artifact_windows']} 个（已排除，不参与评估）\n")

    if args.print_features:
        X, y = dataset["X"], dataset["y"]
        print("类别特征中位数（p10 / p50 / p90）")
        for name in FEATURE_NAMES:
            index = FEATURE_NAMES.index(name)
            cells = []
            for cls in CLASSES:
                v = X[y == cls, index]
                cells.append(f"{cls}: {np.percentile(v, 10):7.2f}/{np.median(v):7.2f}/"
                             f"{np.percentile(v, 90):7.2f}")
            print(f"  {name:<11} " + "   ".join(cells))
        print()

    raw = [raw_classify(f, rule) for f in dataset["features"]]
    predicted = smooth(raw, dataset["times"], dataset["sessions"], hold)
    table = confusion(dataset["y"], predicted)
    print_table(table, "逐窗混淆（行=真值，列=判断；已经状态机平滑）")
    print()
    print_metrics(metrics(table))

    # 留出：每个采集按时间前 60% / 后 40% 切。
    holdout = np.zeros(len(dataset["y"]), dtype=bool)
    for session in np.unique(dataset["sessions"]):
        index = np.where(dataset["sessions"] == session)[0]
        threshold = np.percentile(dataset["times"][index], 60)
        holdout[index] = dataset["times"][index] <= threshold
    print()
    print_table(confusion(dataset["y"][holdout], predicted[holdout]), "前 60% 时间（调参见过）")
    print_metrics(metrics(confusion(dataset["y"][holdout], predicted[holdout])))
    print()
    print_table(confusion(dataset["y"][~holdout], predicted[~holdout]), "后 40% 时间（未参与调参）")
    print_metrics(metrics(confusion(dataset["y"][~holdout], predicted[~holdout])))

    segment_report(dataset, predicted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
