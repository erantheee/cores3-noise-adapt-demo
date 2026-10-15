"""CoreS3 固件对齐的声学特征提取（训练 / 离线复算用）。

对齐对象：
  * main/controller.cpp —— 24 频段 Goertzel、语音四判据（vocal_ratio / peakiness /
    flatness / excited bands + level gate）
  * main/app_main.cpp   —— 48 kHz、2560 样本分析帧、100 ms 决策节拍、512 样本读块

所有常量直接抄自固件，改动必须两边同时改。训练特征与设备特征一旦错位，模型在设备上会
系统性失效，而且不会报错——这是这个项目里最贵的一类错误。

设计原则：模型不引入新的 DSP。设备端每 100 ms 已经算出 24 个频段的 dBFS 与语音描述量，
模型只消费这些量 + 时间 + IMU 运动量，端侧新增的只有一次前向推理。

使用：
    python scripts/features.py <wav> <out.jsonl>     # 逐帧特征落盘（真实录音复核用）
    from features import analyze_frames, window_vector   # 训练管线直接调用
"""

from __future__ import annotations

import argparse
import json
import math
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# 与固件一致的常量（main/controller.cpp / main/app_main.cpp）
# ---------------------------------------------------------------------------

SAMPLE_RATE = 48000
# 固件在决策时刻分析的是"最近累积的整个分析窗"：512 样本一块，最多 5 块 = 2560 样本
# （app_main.cpp 的 kFrameSamplesMax = kFrameSamples + kReadBlockSamples = 2048 + 512），
# 分析后清空。训练必须按同一个帧长取特征，否则频段电平会有约 1 dB 的系统性偏差
# （2560 点帧与 2048 点帧对噪声的幅度归一化差 20·log10(√(2048/2560)) ≈ -0.97 dB）。
FRAME_SAMPLES = 2560          # 53.3 ms 分析帧（固件稳态帧长）
MIN_FRAME_SAMPLES = 2048      # 固件要求至少 2048 才分析
CONTROL_PERIOD_MS = 100.0     # 决策节拍
WINDOW_FRAMES = 20            # 模型窗口：最近 20 帧 = 2.0 s

BAND_CENTER_HZ = np.array([
    70.7, 141.4, 244.9, 346.4, 451.7, 566.8, 696.5, 841.7,
    996.8, 1171.3, 1370.0, 1595.0, 1854.7, 2154.1, 2503.3,
    2916.3, 3414.7, 4036.1, 4829.1, 5824.1, 7020.0, 8552.8,
    10677.1, 13638.2,
], dtype=np.float64)
BAND_COUNT = len(BAND_CENTER_HZ)

# 语音判据阈值（controller.cpp 的 voice_thresholds 命名空间）
GATE_FLOOR_DBFS = -72.0
GATE_FULL_DBFS = -50.0
NOISE_FLATNESS_CEILING = 0.45
MAX_VOCAL_PEAKINESS = 0.72
MIN_VOCAL_RATIO = 0.34
VOCAL_RATIO_SPAN = 0.36
FLOOR_DBFS = -95.0
MIN_EXCITED_VOCAL_BANDS = 5
EXCITED_BAND_SOFTNESS = 2.0
FLATNESS_SOFTNESS = 0.10
PEAK_SOFTNESS = 0.08

# 削波判据（controller.cpp 的 clip_thresholds）
PINNED_VALUE = 32760
MAX_PINNED_RUN = 12
MAX_PINNED_SHARE = 0.20

VOCAL_BANDS = (BAND_CENTER_HZ >= 300.0) & (BAND_CENTER_HZ <= 4000.0)


def clamp01(x):
    return np.clip(x, 0.0, 1.0)


def hann_window(count: int) -> np.ndarray:
    """与固件相同的 Hann 窗：0.5 - 0.5*cos(2πi/(N-1))。"""
    idx = np.arange(count, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * idx / max(count - 1, 1))


def goertzel_dbfs_matrix(frames: np.ndarray, window: np.ndarray) -> np.ndarray:
    """把固件的逐样本 Goertzel 递归向量化到 (帧数, frame_len) 上。

    frames: float64 (n_frames, frame_len)，已经除以 32768；window 与帧长一致。
    返回 (n_frames, BAND_COUNT) 的 dBFS。

    逐位等价于 controller.cpp：coeff = 2cos(ω)；递归 s0 = x + coeff*s1 - s2；
    power = max(s1² + s2² - coeff*s1*s2, 1e-12)；amplitude = 2√power/N；
    dBFS = 20log10(max(amplitude, 1e-6))。
    """
    x = frames * window[None, :]
    n_frames, frame_len = x.shape
    out = np.empty((n_frames, BAND_COUNT), dtype=np.float64)
    for band, freq in enumerate(BAND_CENTER_HZ):
        omega = 2.0 * math.pi * freq / SAMPLE_RATE
        coeff = 2.0 * math.cos(omega)
        s1 = np.zeros(n_frames)
        s2 = np.zeros(n_frames)
        for i in range(frame_len):
            s0 = x[:, i] + coeff * s1 - s2
            s2 = s1
            s1 = s0
        power = np.maximum(s1 * s1 + s2 * s2 - coeff * s1 * s2, 1e-12)
        amplitude = 2.0 * np.sqrt(power) / max(frame_len, 1)
        out[:, band] = 20.0 * np.log10(np.maximum(amplitude, 1e-6))
    return out


def frame_broadband_and_clip(raw: np.ndarray):
    """固件口径：rms 直接来自原始样本（不加窗、不去直流），返回 (broadband_dbfs, mic_valid)。"""
    x = raw.astype(np.float64) / 32768.0
    rms = np.sqrt(np.mean(x * x, axis=-1))
    bb = 20.0 * np.log10(np.maximum(rms, 1e-6))
    pinned = np.abs(raw) >= PINNED_VALUE
    counts = pinned.sum(axis=-1)
    # 最长满幅游程：对每帧求连续 True 的最大长度
    longest = np.zeros(raw.shape[0], dtype=np.int64)
    if raw.shape[0]:
        run = np.zeros(raw.shape[0], dtype=np.int64)
        for i in range(raw.shape[1]):
            run = np.where(pinned[:, i], run + 1, 0)
            longest = np.maximum(longest, run)
    share = counts / raw.shape[1]
    valid = ~((longest > MAX_PINNED_RUN) | (share > MAX_PINNED_SHARE))
    return bb, valid


def voice_features_from_bands(band_dbfs: np.ndarray) -> dict:
    """与 controller.cpp 的 extract_voice_features / voice_likelihood_from_features 对齐。

    输入 (n_frames, 24) 的 dBFS，输出逐帧描述量与 voice_likelihood。
    """
    power = np.power(10.0, band_dbfs / 10.0)
    total_power = power.sum(axis=-1)
    vocal_power = power[:, VOCAL_BANDS].sum(axis=-1)
    vocal_peak = power[:, VOCAL_BANDS].max(axis=-1)
    excited = (band_dbfs[:, VOCAL_BANDS] > FLOOR_DBFS).sum(axis=-1).astype(np.float64)

    vocal_ratio = vocal_power / np.maximum(total_power, 1e-12)
    peakiness = vocal_peak / np.maximum(vocal_power, 1e-12)

    log_sum = np.log(np.maximum(power, 1e-20)).sum(axis=-1)
    geometric = np.exp(log_sum / BAND_COUNT)
    arithmetic = np.maximum(total_power / BAND_COUNT, 1e-20)
    flatness = clamp01(geometric / arithmetic)

    # 谱倾斜：只在可测频段上做最小二乘（固件同口径）
    measurable = band_dbfs > FLOOR_DBFS
    idx = np.arange(BAND_COUNT, dtype=np.float64)[None, :]
    count = np.maximum(measurable.sum(axis=-1), 1)
    mean_idx = (idx * measurable).sum(axis=-1) / count
    mean_lvl = (band_dbfs * measurable).sum(axis=-1) / count
    dx = (idx - mean_idx[:, None]) * measurable
    covariance = (dx * (band_dbfs - mean_lvl[:, None])).sum(axis=-1)
    variance = (dx * dx).sum(axis=-1)
    tilt = covariance / np.maximum(variance, 1e-6)

    return {
        "vocal_ratio": vocal_ratio,
        "vocal_peakiness": peakiness,
        "excited_vocal_bands": excited,
        "spectral_tilt": tilt,
        "spectral_flatness": flatness,
    }


def voice_likelihood(bands: np.ndarray, broadband: np.ndarray, f: dict) -> np.ndarray:
    band_term = clamp01((f["vocal_ratio"] - MIN_VOCAL_RATIO) / VOCAL_RATIO_SPAN)
    peak_term = clamp01((MAX_VOCAL_PEAKINESS - f["vocal_peakiness"]) / PEAK_SOFTNESS)
    noise_term = clamp01((NOISE_FLATNESS_CEILING - f["spectral_flatness"]) / FLATNESS_SOFTNESS)
    spread_term = clamp01(
        (f["excited_vocal_bands"] - (MIN_EXCITED_VOCAL_BANDS - EXCITED_BAND_SOFTNESS))
        / EXCITED_BAND_SOFTNESS
    )
    level_gate = clamp01((broadband - GATE_FLOOR_DBFS) / (GATE_FULL_DBFS - GATE_FLOOR_DBFS))
    return band_term * peak_term * noise_term * spread_term * level_gate


def analyze_frames(samples: np.ndarray, period_ms: float = CONTROL_PERIOD_MS,
                   frame_samples: int = FRAME_SAMPLES):
    """按固件口径切帧并算出逐帧特征。

    samples: int16 一维数组（48 kHz）。
    每个决策时刻（每 period_ms 一次），固件分析的是它刚攒满的分析窗；本函数用
    "截至该时刻的最后 frame_samples 个样本"复刻同一窗口（帧长一致，窗口位置最多差半个
    读块 ≈ 5–11 ms，对 2 秒统计量不构成系统性偏差）。
    返回 dict of arrays，帧数与时间轴一一对应。
    """
    step = int(round(SAMPLE_RATE * period_ms / 1000.0))
    n_frames = len(samples) // step            # 每帧都恰好结束在决策时刻上
    if n_frames <= 0 or len(samples) < frame_samples:
        return None
    starts = np.arange(n_frames) * step + (step - frame_samples)
    raw = np.stack([samples[s:s + frame_samples] for s in starts]).astype(np.int64)
    broadband, mic_valid = frame_broadband_and_clip(raw)
    window = hann_window(frame_samples)
    bands = goertzel_dbfs_matrix(raw.astype(np.float64) / 32768.0, window)
    feats = voice_features_from_bands(bands)
    vl = voice_likelihood(bands, broadband, feats)
    return {
        "t_s": (starts + frame_samples) / SAMPLE_RATE,
        "broadband_dbfs": broadband,
        "band_dbfs": bands,
        "voice_likelihood": vl,
        "mic_valid": mic_valid,
        **feats,
    }


# ---------------------------------------------------------------------------
# 模型输入向量（窗特征）
# ---------------------------------------------------------------------------

FEATURE_NAMES = (
    [f"band_mean_{i:02d}" for i in range(BAND_COUNT)]
    + [f"band_std_{i:02d}" for i in range(BAND_COUNT)]
    + [f"band_slope_{i:02d}" for i in range(BAND_COUNT)]
    + ["level_mean", "level_std", "level_min", "level_max"]
    + ["vl_mean", "vl_std", "vl_active_frac", "vl_transitions"]
    + ["flatness_mean", "peakiness_mean", "vocal_ratio_mean", "excited_mean"]
    + ["level_steps"]
    + ["motion_mean", "motion_std"]
    + ["hour_sin", "hour_cos", "dow_sin", "dow_cos", "is_weekend"]
)
FEATURE_COUNT = len(FEATURE_NAMES)


def window_vector(frames: dict, i0: int, i1: int, motion: np.ndarray | None = None,
                  when: datetime | None = None) -> np.ndarray:
    """把 [i0, i1) 的帧压成一个特征向量。索引越界自动截断。"""
    i0 = max(0, i0)
    i1 = min(len(frames["t_s"]), i1)
    bands = frames["band_dbfs"][i0:i1]           # (w, 24)
    times = frames["t_s"][i0:i1]
    n = bands.shape[0]
    vec = np.zeros(FEATURE_COUNT, dtype=np.float64)
    if n == 0:
        return vec

    band_mean = bands.mean(axis=0)
    band_std = bands.std(axis=0)
    if n >= 2:
        t = times - times.mean()
        denom = float(np.sum(t * t)) or 1.0
        band_slope = (bands * t[:, None]).sum(axis=0) / denom
    else:
        band_slope = np.zeros(BAND_COUNT)

    level = frames["broadband_dbfs"][i0:i1]
    vl = frames["voice_likelihood"][i0:i1]
    active = vl > 0.55
    transitions = float(np.count_nonzero(active[1:] != active[:-1])) if n > 1 else 0.0
    steps = float(np.count_nonzero(np.abs(np.diff(level)) > 3.0)) if n > 1 else 0.0

    if motion is None:
        motion_win = np.zeros(n)
    else:
        motion_win = np.asarray(motion)[i0:i1]
        if motion_win.size != n:
            motion_win = np.zeros(n)

    if when is not None:
        hour = when.hour + when.minute / 60.0
        hour_frac = hour / 24.0
        dow = when.weekday()
    else:
        hour_frac = 0.5
        dow = 2

    vec[:] = np.concatenate([
        band_mean, band_std, band_slope,
        [level.mean(), level.std(), level.min(), level.max()],
        [vl.mean(), vl.std(), float(active.mean()), transitions],
        [frames["spectral_flatness"][i0:i1].mean(),
         frames["vocal_peakiness"][i0:i1].mean(),
         frames["vocal_ratio"][i0:i1].mean(),
         frames["excited_vocal_bands"][i0:i1].mean()],
        [steps],
        [motion_win.mean(), motion_win.std()],
        [math.sin(2 * math.pi * hour_frac), math.cos(2 * math.pi * hour_frac),
         math.sin(2 * math.pi * dow / 7.0), math.cos(2 * math.pi * dow / 7.0),
         1.0 if dow >= 5 else 0.0],
    ])
    return vec


def load_wav_mono(path: Path) -> np.ndarray:
    """读单声道 WAV，统一转成 int16 量纲。

    支持两种现场会遇到的格式：PCM16（设备录音、合成语料）与 float32（ows-earbud-demo 的
    素材就是 float32）。48 kHz 是硬性要求——本管线的 24 频段特征是按 48 kHz 标定的，
    遇到别的采样率直接报错，不做静默重采样（重采样会改变频段能量，等于换了特征）。
    """
    blob = Path(path).read_bytes()
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise SystemExit(f"{path}: 不是 RIFF/WAVE")
    fmt = data = None
    pos = 12
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        size = int.from_bytes(blob[pos + 4:pos + 8], "little")
        chunk = blob[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = chunk
        elif cid == b"data":
            data = chunk
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise SystemExit(f"{path}: 缺少 fmt/data 块")
    tag = int.from_bytes(fmt[0:2], "little")
    channels = int.from_bytes(fmt[2:4], "little")
    rate = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if tag == 0xFFFE and len(fmt) >= 26:          # WAVE_FORMAT_EXTENSIBLE
        tag = int.from_bytes(fmt[24:26], "little")
    if channels < 1 or channels > 8:
        raise SystemExit(f"{path}: 声道数不合理 {channels}")
    if rate != SAMPLE_RATE:
        raise SystemExit(f"{path}: {rate} Hz，需要 {SAMPLE_RATE} Hz（本管线不做重采样）")
    if tag == 1 and bits == 16:
        samples = np.frombuffer(data, dtype="<i2").reshape(-1, channels).astype(np.float64)
    elif tag == 3 and bits == 32:
        samples = np.frombuffer(data, dtype="<f4").reshape(-1, channels).astype(np.float64)
        samples = np.clip(samples, -1.0, 1.0) * 32767.0
    else:
        raise SystemExit(f"{path}: 不支持的格式 tag={tag} bits={bits}")
    if channels > 1:                      # 多声道降混（音乐素材是立体声）
        samples = samples.mean(axis=1)
    else:
        samples = samples[:, 0]
    return samples.astype(np.int16)


def main():
    ap = argparse.ArgumentParser(description="逐帧特征提取（与固件对齐）")
    ap.add_argument("wav")
    ap.add_argument("out")
    args = ap.parse_args()
    samples = load_wav_mono(Path(args.wav))
    frames = analyze_frames(samples)
    if frames is None:
        raise SystemExit("音频太短")
    out = Path(args.out)
    with out.open("w") as fh:
        for i in range(len(frames["t_s"])):
            fh.write(json.dumps({
                "t_s": round(float(frames["t_s"][i]), 4),
                "broadband_dbfs": round(float(frames["broadband_dbfs"][i]), 3),
                "band_dbfs": [round(float(v), 3) for v in frames["band_dbfs"][i]],
                "voice_likelihood": round(float(frames["voice_likelihood"][i]), 4),
                "mic_valid": bool(frames["mic_valid"][i]),
            }, ensure_ascii=False) + "\n")
    print(f"{len(frames['t_s'])} 帧 -> {out}")


if __name__ == "__main__":
    main()
