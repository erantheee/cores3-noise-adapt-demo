#!/usr/bin/env python3
"""把 ear-context 采到的真机会话导出成 bone_real_replay 能吃的 CSV。

为什么需要它：bone_voice 的阈值全是在合成正弦上定的，而这个仓库里有一条**否定结论**
（experiments/ear-context/selfvoice_report.py：11 个 IMU 特征做 LOSO 分类，AUC 0.0508，
"基本不可分，建议停"）。我的判据与它不同（振动包络 × 音频包络的相关），但必须先在同一份
真机数据上验一遍，再谈真机联调。

会话目录里的东西：
  samples.jsonl  100 Hz 六轴（accel_mps2 含重力）+ host_mono_ns
  labels.jsonl   分段标签，与 samples 同一个 host 单调时钟
  audio.wav      宿主麦克风录音（**不是设备麦克风**），t=0 不可信

对齐：音频的 t=0 与设备时钟无关，所以用"标签轮廓"做对比度搜索：取说话段与非说话段的
包络均值之差最大的偏移。会话记录里也提到用两次敲桌子对齐；本脚本用对比度是因为它不依赖
手工找脉冲，且结果可复现（见输出里的对比度曲线，峰不尖锐时会明说）。

用法：
  .venv/bin/python tools/export_bone_real.py <session_dir> <out.csv>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import wave
from scipy.signal import butter, sosfiltfilt


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    session, out_path = Path(sys.argv[1]), Path(sys.argv[2])

    raw, accel = [], []
    for line in (session / "samples.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("type") != "imu":
            continue
        raw.append(r["host_mono_ns"] / 1e9)
        accel.append(r["accel_mps2"])
    raw = np.asarray(raw)
    accel = np.asarray(accel)
    t = raw - raw[0]
    print(f"IMU {len(t)} 样本，{t[-1]:.1f}s，{len(t) / t[-1]:.1f} Hz")

    labels = [(r["label"], r["start_mono_ns"] / 1e9 - raw[0], r["end_mono_ns"] / 1e9 - raw[0])
              for r in (json.loads(l) for l in (session / "labels.jsonl").open(encoding="utf-8"))]

    with wave.open(str(session / "audio.wav")) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    sos = butter(4, [300 / (sr / 2), 4000 / (sr / 2)], btype="band", output="sos")
    xb = sosfiltfilt(sos, x)
    hop = sr // 10
    frames = np.lib.stride_tricks.sliding_window_view(xb, hop)[::hop]
    env = 10 * np.log10(np.maximum((frames ** 2).mean(axis=1), 1e-12))
    ta = np.arange(len(env)) / 10.0
    k = 300
    envd = env - np.convolve(env, np.ones(k) / k, mode="same")   # 去慢漂移

    speaking = np.zeros(len(t))
    for name, s0, e0 in labels:
        if name in ("self_speech", "speech_music"):
            speaking[(t >= s0) & (t < e0)] = 1.0
    scored = []
    for off in np.arange(-30, 30.001, 0.5):
        e = np.interp(t, ta + off, envd, left=0.0, right=0.0)
        scored.append((off, float(e[speaking == 1].mean() - e[speaking == 0].mean())))
    scored.sort(key=lambda z: -z[1])
    off, contrast = scored[0]
    at_zero = dict(scored).get(0.0, float("nan"))
    print(f"对齐偏移 δ={off:+.1f}s，对比度 {contrast:.2f} dB（δ=0 时 {at_zero:.2f} dB）")
    if contrast - at_zero < 1.0:
        print("  ⚠️ 对比度曲线在 δ=0 附近几乎持平：这条对齐不尖锐，结论只能当趋势看")

    env_on = np.interp(t, ta + off, env, left=-120.0, right=-120.0)
    label_of = np.array(["unknown"] * len(t), dtype=object)
    for name, s0, e0 in labels:
        label_of[(t >= s0) & (t < e0)] = name

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as sink:
        sink.write("t_ms,accel_mag,audio_tick_dbfs,label\n")
        for i in range(len(t)):
            audio = f"{env_on[i]:.2f}" if i % 10 == 0 else ""
            sink.write(f"{t[i] * 1000:.1f},{np.linalg.norm(accel[i]):.4f},{audio},{label_of[i]}\n")
    print(f"已导出 {out_path}（{len(t)} 行 @100 Hz）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
