"""官方 YAMNet（float / SavedModel）在 Mac 上当教师：0.96 s 窗，正好适合事件级标签。

之前试过社区 INT8 TFLM 版（图外 mel），输出不可信；官方版吃原始波形、图内做 mel，
结果正常（TTS 语音 → Speech 1.00）。它只在 Mac 上跑，不参与端侧链路。

时间分辨率：0.96 s 窗、0.48 s 步长，比 AST 的 10.24 s 适合键盘/风/敲桌这类短事件。
输出：每个 0.48 s 一个 521 类概率向量；本脚本只保留与本项目相关的目标类，并写 JSONL。

用法（需要 teacher venv：tensorflow 2.16 + numpy<2）：
    experiments/sound-events/.venv-teacher/bin/python \
        experiments/sound-events/teacher_yamnet_official.py <wav> --json out.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
#: 目标类：display name 必须与 yamnet_class_map.csv 完全一致。
TARGET_NAMES = {
    "typing": "Typing",
    "computer_keyboard": "Computer keyboard",
    "clickety": "Clickety-clack",
    "wind": "Wind",
    "wind_mic": "Wind noise (microphone)",
    "knock": "Knock",
    "tap": "Tap",
    "speech": "Speech",
    "silence": "Silence",
    "noise": "Noise",
    "music": "Music",
    "fan": "Mechanical fan",
    "vacuum": "Vacuum cleaner",
}


def load_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
            raise SystemExit(f"{path}: 需要 16 kHz 单声道")
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: 需要 16-bit PCM")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def class_names(path: Path) -> list[str]:
    names = []
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        for row in reader:
            names.append(row[2])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="官方 YAMNet 教师（0.96 s 窗）")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--class-map", type=Path,
                        default=Path(__file__).resolve().parent / "teacher" /
                        "yamnet_class_map.csv")
    args = parser.parse_args()

    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    # 这个隔离 venv 里 distutils-precedence.pth 有时不执行（Python 3.12 已移除 distutils，
    # TensorFlow 2.16 仍会 import 它）。显式装回 setuptools 的 shim，别让教师依赖启动脚本的运气。
    try:
        import distutils  # noqa: F401
    except ModuleNotFoundError:
        import _distutils_hack
        _distutils_hack.add_shim()
    import tensorflow_hub as hub
    import tensorflow as tf

    model = hub.load("https://tfhub.dev/google/yamnet/1")
    signature = model.signatures["serving_default"]
    names = class_names(args.class_map)
    indices = {}
    for key, display in TARGET_NAMES.items():
        try:
            indices[key] = names.index(display)
        except ValueError:
            print(f"!! 类目表里没有 {display!r}", flush=True)

    audio = load_wav_mono(args.wav)
    output = signature(tf.constant(audio))
    scores = np.asarray(output["output_0"])
    rows = []
    for patch in range(scores.shape[0]):
        start_s = patch * 0.48
        top = np.argsort(scores[patch])[::-1][:args.top]
        row = {
            "start_s": round(start_s, 3),
            "center_s": round(start_s + 0.48, 3),
            "top": [{"class": names[int(i)], "score": round(float(scores[patch, i]), 4)}
                    for i in top],
            "targets": {key: round(float(scores[patch, index]), 4)
                        for key, index in indices.items()},
        }
        rows.append(row)
    print(f"{args.wav}: {len(rows)} 个 patch（0.96 s / 步长 0.48 s）")
    for row in rows:
        top = ", ".join(f"{item['class']} {item['score']:.2f}" for item in row["top"][:4])
        targets = row["targets"]
        print(f"  {row['center_s']:6.2f}s  {top}   "
              f"[typing {targets.get('typing', 0):.2f} wind {targets.get('wind_mic', 0):.2f} "
              f"knock {targets.get('knock', 0):.2f} speech {targets.get('speech', 0):.2f}]")
    if args.json:
        with args.json.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"JSONL -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
