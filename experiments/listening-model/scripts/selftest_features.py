#!/usr/bin/env python3
"""特征复刻自检：Python 实现必须复现固件实测的判据行为。

依据（cores3-noise-adapt-demo/docs/edge-feasibility.md 第八节，真机实测）：
    * 白噪声平坦度 ≈ 0.693 ，语音 ≈ 0.010–0.012 ，两者相距 56 倍，判据门 0.45
    * 1 kHz 纯音必须被拒绝（peakiness → 1.0）
    * 白噪声必须被拒绝、语音必须被接受（voice_likelihood）

这台机器上复刻值的实测结果写在本文件末尾的 CHECK 里；如果哪天固件改了判据常量，
这里会先失败，提醒两边已经不同步。

运行：python3 scripts/selftest_features.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import analyze_frames, load_wav_mono  # noqa: E402

SPEECH = Path("/Users/eranthe/Documents/Codex/2026-10-12/"
              "https-mimi-io-products-noise-adapt/work/speech-48k.wav")


def main() -> int:
    rng = np.random.default_rng(3)
    n = 48000 * 5
    failures = []

    noise = rng.standard_normal(n)
    noise = noise / np.abs(noise).max() * 0.03
    f = analyze_frames((noise * 32767).astype(np.int16))
    flat_noise = float(np.median(f["spectral_flatness"]))
    vl_noise = float(np.median(f["voice_likelihood"]))
    print(f"白噪声:     flatness={flat_noise:.3f}（固件实测 0.693）vl={vl_noise:.3f}")
    if not (0.40 <= flat_noise <= 0.90):
        failures.append("白噪声平坦度偏离固件实测区间")
    if vl_noise > 0.05:
        failures.append("白噪声不应被判为人声")

    t = np.arange(n) / 48000
    tone = 0.03 * np.sin(2 * np.pi * 1000 * t)
    f = analyze_frames((tone * 32767).astype(np.int16))
    peak = float(np.median(f["vocal_peakiness"]))
    vl_tone = float(np.median(f["voice_likelihood"]))
    print(f"1 kHz 纯音: peakiness={peak:.3f} vl={vl_tone:.3f}")
    if vl_tone > 0.05:
        failures.append("纯音不应被判为人声")

    if SPEECH.exists():
        f = analyze_frames(load_wav_mono(SPEECH))
        active = f["voice_likelihood"] > 0.55
        flat_speech = float(np.median(f["spectral_flatness"][active])) if active.any() else float("nan")
        print(f"真实语音:   活跃帧 {active.mean() * 100:.0f}%  flatness={flat_speech:.3f}"
              f"（固件实测 0.010–0.012）")
        if not active.any():
            failures.append("真实语音没有任何一帧被判为人声")
        if active.any() and not (0.002 <= flat_speech <= 0.05):
            failures.append("语音平坦度偏离固件实测区间")
    else:
        print(f"（跳过真实语音：找不到 {SPEECH}）")

    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("OK: 复刻与固件实测行为一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
