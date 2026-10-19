"""键盘 / 风声事件判断的特征口径（离线原型；C++ 固件按同一口径实现）。

设计约束（决定了所有取舍）：

* **不改 DSP。** 只消费固件每 100 ms 已经在算的 24 段 dBFS 与宽带电平。
* **特征必须是"能搬进 C++"的。** 这里只用加减乘除、exp/log、比较；不用分位数、LPC、FFT。
* **基线用噪声地板跟踪，不用"安静段中位数"。** 固件开机时不知道哪段是安静；离线如果
  用 quiet 段算基线，训练口径和固件口径就错位（这个仓库最贵的一类错误）。所以这里实现
  一个向下快、向上慢的地板跟踪器，训练与固件都用它。

帧率 10 Hz，窗口 10 帧（1 s）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BAND_CENTER_HZ = np.array([
    70.7, 141.4, 244.9, 346.4, 451.7, 566.8, 696.5, 841.7,
    996.8, 1171.3, 1370.0, 1595.0, 1854.7, 2154.1, 2503.3,
    2916.3, 3414.7, 4036.1, 4829.1, 5824.1, 7020.0, 8552.8,
    10677.1, 13638.2,
], dtype=np.float64)
BAND_COUNT = 24
VOCAL_BANDS = (BAND_CENTER_HZ >= 300.0) & (BAND_CENTER_HZ <= 4000.0)
WINDOW_FRAMES = 10
FEATURE_NAMES = [
    "d_lo", "d_mid", "d_hi", "mid_lo", "hi_mid",
    "cent", "flat", "vr", "std_lo", "range_lo", "std_mid", "level_rel",
    "vocal_peak", "slope", "low_share",
]
CLASSES = ["quiet", "speech", "keyboard", "wind"]

#: 训练标签 -> 四类。准备/停顿/结束都算 quiet（没有事件）。
PHASE_TO_CLASS = {
    "ready": "quiet", "quiet": "quiet", "pause": "quiet", "done": "quiet",
    "speech": "speech", "keyboard": "keyboard", "wind": "wind",
}

#: 地板跟踪器：向下快（跟得上安静下来），向上慢（不被说话/风抬走）。
FLOOR_DOWN = 0.25
FLOOR_UP = 0.002
#: 初始化用多少帧的中位数。
FLOOR_INIT_FRAMES = 10


@dataclass
class Frame:
    band_dbfs: np.ndarray
    broadband_dbfs: float
    mic_valid: bool


def frame_descriptors(band_dbfs: np.ndarray) -> tuple[float, float, float, float, float, float, float]:
    """逐帧描述量：质心、平坦度、语音带占比、语音带峰值集中度、频段斜率、低频能量占比。

    全部只用到 24 段 dBFS：`power = 10^(dB/10)`，没有额外 DSP。
    """
    power = np.power(10.0, band_dbfs / 10.0)
    total = max(float(power.sum()), 1e-20)
    centroid = float((power * BAND_CENTER_HZ).sum() / total)
    flatness = float(np.exp(np.log(np.maximum(power, 1e-20)).mean()) / (total / BAND_COUNT))
    vocal_power = power[VOCAL_BANDS]
    vocal_sum = max(float(vocal_power.sum()), 1e-20)
    vocal_ratio = float(vocal_sum / total)
    vocal_peak = float(vocal_power.max() / vocal_sum)
    # 频段序号对 dB 的最小二乘斜率（与固件 spectral_tilt_db_per_band 同口径）。
    index = np.arange(BAND_COUNT, dtype=np.float64)
    centered = index - index.mean()
    slope = float((centered * (band_dbfs - band_dbfs.mean())).sum() / (centered * centered).sum())
    low_share = float(power[:5].sum() / total)
    return centroid, flatness, vocal_ratio, vocal_peak, slope, low_share


class FloorTracker:
    """24 段噪声地板跟踪器。固件侧逐帧调用 `step()`，离线逐帧回放同一实现。"""

    def __init__(self) -> None:
        self.base: np.ndarray | None = None
        self.level: float = -120.0
        self._init: list[np.ndarray] = []
        self._init_levels: list[float] = []

    def step(self, band_dbfs: np.ndarray, broadband_dbfs: float, mic_valid: bool) -> None:
        if not mic_valid:
            return
        if self.base is None:
            self._init.append(band_dbfs.astype(np.float64))
            self._init_levels.append(float(broadband_dbfs))
            if len(self._init) >= FLOOR_INIT_FRAMES:
                self.base = np.median(np.stack(self._init), axis=0)
                self.level = float(np.median(self._init_levels))
            return
        delta = band_dbfs - self.base
        rate = np.where(delta < 0.0, FLOOR_DOWN, FLOOR_UP)
        self.base = self.base + rate * delta
        dlevel = float(broadband_dbfs) - self.level
        self.level += (FLOOR_DOWN if dlevel < 0.0 else FLOOR_UP) * dlevel

    @property
    def ready(self) -> bool:
        return self.base is not None


def load_capture(path: Path) -> list[tuple[Frame, str, float]]:
    """读 `tools/live_band_capture.py` 的 CSV：[(帧, 阶段名, 秒)]。"""
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    out = []
    for row in rows:
        band = np.array([float(row[f"band_{i:02d}"]) for i in range(BAND_COUNT)])
        power = np.power(10.0, band / 10.0)
        broadband = 10.0 * np.log10(max(float(power.sum()), 1e-12))
        valid = row["context_valid"] == "True"
        out.append((Frame(band, broadband, valid), row["phase"], float(row["elapsed_s"])))
    return out


def window_features(base: np.ndarray, base_level: float, frames: list[Frame]) -> dict[str, float]:
    """10 帧窗口 -> 12 维事件特征。与固件侧同一口径。"""
    bands = np.stack([f.band_dbfs for f in frames])
    rel = bands - base
    d_lo = float(rel[:, :5].mean())
    d_mid = float(rel[:, 5:16].mean())
    d_hi = float(rel[:, 16:].mean())
    env_lo = bands[:, :5].mean(axis=1)
    env_mid = bands[:, 5:16].mean(axis=1)

    # 逐帧描述量：固件同样每帧算一次，窗口里取均值（不必逐帧存 24 段）。
    cursor = np.zeros(7)
    for frame in frames:
        centroid, flatness, vocal_ratio, vocal_peak, slope, low_share = frame_descriptors(
            frame.band_dbfs)
        cursor += np.array([centroid, flatness, vocal_ratio, frame.broadband_dbfs,
                            vocal_peak, slope, low_share])
    cursor /= len(frames)

    return {
        "d_lo": d_lo,
        "d_mid": d_mid,
        "d_hi": d_hi,
        "mid_lo": d_mid - d_lo,
        "hi_mid": d_hi - d_mid,
        "cent": float(cursor[0]),
        "flat": float(cursor[1]),
        "vr": float(cursor[2]),
        "std_lo": float(env_lo.std()),
        "range_lo": float(env_lo.max() - env_lo.min()),
        "std_mid": float(env_mid.std()),
        "level_rel": float(cursor[3] - base_level),
        "vocal_peak": float(cursor[4]),
        "slope": float(cursor[5]),
        "low_share": float(cursor[6]),
    }


def build_dataset(paths: list[Path], hop: int = 1, guard_s: float = 2.5,
                  warmup_s: float = 20.0) -> dict:
    """回放采集 CSV，产出窗口特征 + 标签 + 分组（5 s block，供分组交叉验证）。

    只保留整窗有效（无撞击/窗未满）且不在段首 guard 窗内的样本。撞击窗单独计数，
    训练与评估都不含它们——事件判断必须先过撞击门控，这一段由 `mic_valid` 负责。
    """
    features, labels, groups, sessions, times = [], [], [], [], []
    artifacts = 0
    for session_index, path in enumerate(paths):
        frames = load_capture(path)
        tracker = FloorTracker()
        current_phase = frames[0][1]
        phase_start = frames[0][2]
        for index, (frame, phase_name, t) in enumerate(frames):
            if phase_name != current_phase:
                current_phase = phase_name
                phase_start = t
            tracker.step(frame.band_dbfs, frame.broadband_dbfs, frame.mic_valid)
            if not tracker.ready or index + 1 < WINDOW_FRAMES:
                continue
            window = [f for f, _, _ in frames[index - WINDOW_FRAMES + 1:index + 1]]
            valid = all(f.mic_valid for f in window)
            guarded = t - phase_start < guard_s or t < warmup_s
            if not valid:
                artifacts += 1
                continue
            if guarded:
                continue
            features.append(window_features(tracker.base, tracker.level, window))
            labels.append(PHASE_TO_CLASS.get(phase_name, phase_name))
            groups.append(session_index * 1000 + int(t // 5))
            sessions.append(session_index)
            times.append(t)
    X = np.array([[f[name] for name in FEATURE_NAMES] for f in features], dtype=np.float64)
    return {
        "X": X,
        "y": np.array(labels),
        "groups": np.array(groups),
        "sessions": np.array(sessions),
        "times": np.array(times),
        "features": features,
        "artifact_windows": artifacts,
    }
