#!/usr/bin/env python3
"""Build a live-conditions voiceprint enrollment from recorded training segments.

2026-10-26 现场：10-19 的注册原型对今天的现场麦/房间条件打分最高只有 ~0.67
（阈值 0.8），self 一次都判不出来。用现场录到的本人语音重建注册集，让原型和
判决同域。

用法（先备份旧注册目录）：
  .venv/bin/python tools/enroll_from_segments.py \
      --training-dir 记录/训练数据/20261026-1710xx --segments 12,13,14 \
      --out-dir 记录/声纹/self-device-live

每段会按能量 VAD 裁掉首尾静音后落成 16 kHz 单声道 WAV（与 worker 注册口径一致）。
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from host.assistant import AssistantService  # noqa: E402


def load_wav(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: 需要 16-bit PCM")
        if handle.getnchannels() != 1:
            raise SystemExit(f"{path}: 需要单声道")
        return handle.getframerate(), handle.readframes(handle.getnframes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-dir", required=True)
    parser.add_argument("--segments", required=True,
                        help="训练段号，逗号分隔（例如 12,13,14）")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--min-speech-s", type=float, default=1.2)
    args = parser.parse_args(argv)

    root = Path(args.training_dir)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for raw in args.segments.split(","):
        number = int(str(raw).strip())
        wav = root / "audio" / f"seg{number:04d}.wav"
        if not wav.exists():
            print(f"跳过 seg{number:04d}：没有 {wav}")
            continue
        rate, pcm = load_wav(wav)
        regions = AssistantService._audio_speech_regions(pcm, rate)
        trimmed, _ = AssistantService._trim_to_speech(pcm, rate, regions)
        seconds = len(trimmed) / 2 / rate if trimmed else 0.0
        if seconds < args.min_speech_s:
            print(f"跳过 seg{number:04d}：有效语音只有 {seconds:.2f}s")
            continue
        target = out / f"live-{number:04d}.wav"
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(trimmed)
        written += 1
        print(f"写入 {target}（{seconds:.2f}s）")
    print(f"完成：{written} 条注册音频 → {out}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
