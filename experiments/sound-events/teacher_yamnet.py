"""YAMNet INT8（TFLite Micro 版）在 Mac 上当教师：给真机 16 kHz 录音打 AudioSet 标签。

为什么是"教师"而不是端侧运行时：这个模型 3.75 M 参数 / 4 MB INT8 / ~1.2 MB arena，
在 ESP32-S3 上约 500 ms/0.96 s patch（社区数据）——能跑，但只能 1–2 次/秒，延迟 ≥1.5 s。
它的用处是给"设备采到的真实音频"自动标注，再把这些软标签对齐到 10 Hz 频段特征上，
蒸馏出一个微秒级的学生模型（见 `docs/decisions.md` D18、`docs/plan-status.md` 第 49 项）。

输入口径（照模型卡）：16 kHz 单声道、25 ms 窗 / 10 ms hop、512 点 FFT、64 带 mel
（125–7500 Hz）、log(mel + 1e-10)、96 帧一个 patch（0.96 s），每 48 帧出一个 patch。
输出：521 个 AudioSet 类别的 sigmoid 概率（INT8 反量化后）。

与官方 YAMNet 的差异（写清楚，便于以后换模型时对齐）：官方导出的 tflite 吃原始波形并在
图内做 mel；这个社区版把 mel 前端拿到图外（因为 TFLM 没有 `AUDIO_SPECTROGRAM` op）。
因此本文件里的 FFT/Hann/mel 实现就是"训练侧口径"，改动会让教师的标签跟着变。

用法：
    .venv/bin/python experiments/sound-events/teacher_yamnet.py 记录/教师数据/test-01/audio.wav
    ... --json /tmp/teacher.jsonl --top 8
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
FRAME_LENGTH = 400      # 25 ms
FRAME_HOP = 160         # 10 ms
FFT_LENGTH = 512
MEL_BANDS = 64
MEL_LOW_HZ = 125.0
MEL_HIGH_HZ = 7500.0
PATCH_FRAMES = 96       # 0.96 s
PATCH_HOP_FRAMES = 48   # 0.48 s
LOG_EPS = 1e-10

#: 与本项目四类相关的 AudioSet 类目下标（见 yamnet_class_map.csv）。
TARGET_CLASSES = {
    "typing": 378,             # Typing
    "computer_keyboard": 380,  # Computer keyboard
    "clickety": 486,           # Clickety-clack
    "wind": 277,               # Wind
    "wind_mic": 279,           # Wind noise (microphone)
    "knock": 353,              # Knock
    "tap": 354,                # Tap
    "speech": 0,               # Speech
    "silence": 494,            # Silence
    "noise": 507,              # Noise
    "music": 132,              # Music
}


def load_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
            raise SystemExit(
                f"{path}: 需要 16 kHz 单声道，实得 {handle.getframerate()} Hz / "
                f"{handle.getnchannels()} 声道")
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: 需要 16-bit PCM")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


def mel_filterbank() -> np.ndarray:
    """(FFT_LENGTH/2+1, MEL_BANDS) 的三角滤波器组，HTK mel 刻度。"""
    mel_points = np.linspace(hz_to_mel(MEL_LOW_HZ), hz_to_mel(MEL_HIGH_HZ), MEL_BANDS + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((FFT_LENGTH + 1) * hz_points / SAMPLE_RATE).astype(int)
    filters = np.zeros((FFT_LENGTH // 2 + 1, MEL_BANDS))
    for band in range(MEL_BANDS):
        left, center, right = bins[band], bins[band + 1], bins[band + 2]
        if center == left:
            center = min(left + 1, FFT_LENGTH // 2)
        if right == center:
            right = min(center + 1, FFT_LENGTH // 2 + 1)
        filters[left:center, band] = (np.arange(left, center) - left) / max(center - left, 1)
        filters[center:right, band] = (right - np.arange(center, right)) / max(right - center, 1)
    return filters


def log_mel(audio: np.ndarray) -> np.ndarray:
    """(帧数, 64) 的 log-mel，口径见文件头。"""
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(FRAME_LENGTH) / FRAME_LENGTH)
    frame_count = 1 + max(0, (len(audio) - FRAME_LENGTH) // FRAME_HOP)
    frames = np.stack([
        audio[i * FRAME_HOP: i * FRAME_HOP + FRAME_LENGTH] * window
        for i in range(frame_count)
    ]) if frame_count else np.zeros((0, FRAME_LENGTH))
    spectrum = np.fft.rfft(frames, n=FFT_LENGTH, axis=1)
    power = np.abs(spectrum) ** 2
    mel = power @ mel_filterbank()
    return np.log(mel + LOG_EPS)


class YamnetTeacher:
    def __init__(self, model_path: Path, class_map_path: Path) -> None:
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "缺少 TFLite 运行时：uv pip install --python .venv/bin/python ai-edge-litert"
            ) from exc
        self.interpreter = Interpreter(model_path=str(model_path))
        self.interpreter.allocate_tensors()
        self.input_detail = self.interpreter.get_input_details()[0]
        self.output_detail = self.interpreter.get_output_details()[0]
        if list(self.input_detail["shape"]) != [1, PATCH_FRAMES, MEL_BANDS]:
            raise SystemExit(f"模型输入 {self.input_detail['shape']} 与实现的 mel patch 不一致")
        self.class_names = []
        with class_map_path.open(encoding="utf-8") as handle:
            header = handle.readline()
            del header
            for line in handle:
                parts = line.rstrip("\n").split(",", 2)
                self.class_names.append(parts[2].strip('"') if len(parts) > 2 else parts[1])

    def predict_patch(self, patch: np.ndarray) -> np.ndarray:
        scale, zero_point = self.input_detail["quantization"]
        quantized = np.round(patch / scale + zero_point).astype(np.int8)
        self.interpreter.set_tensor(self.input_detail["index"], quantized[None, ...])
        self.interpreter.invoke()
        raw = self.interpreter.get_tensor(self.output_detail["index"])[0].astype(np.int32)
        out_scale, out_zero = self.output_detail["quantization"]
        return out_scale * (raw - out_zero)


def analyse(wav_path: Path, model_dir: Path, top: int) -> list[dict]:
    audio = load_wav_mono(wav_path)
    mel = log_mel(audio)
    teacher = YamnetTeacher(model_dir / "yamnet.tflite", model_dir / "yamnet_class_map.csv")
    patches = []
    for start in range(0, max(0, len(mel) - PATCH_FRAMES + 1), PATCH_HOP_FRAMES):
        patch = mel[start:start + PATCH_FRAMES]
        probs = teacher.predict_patch(patch)
        center_s = (start + PATCH_FRAMES / 2.0) * FRAME_HOP / SAMPLE_RATE
        order = np.argsort(probs)[::-1][:top]
        patches.append({
            "start_s": round(start * FRAME_HOP / SAMPLE_RATE, 3),
            "center_s": round(center_s, 3),
            "top": [{"class": teacher.class_names[i], "index": int(i),
                     "score": round(float(probs[i]), 4)} for i in order],
            "targets": {name: round(float(probs[index]), 4)
                        for name, index in TARGET_CLASSES.items()},
        })
    return patches


def main() -> int:
    parser = argparse.ArgumentParser(description="YAMNet 教师：给真机 16 kHz 录音打标签")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--model-dir", type=Path,
                        default=Path(__file__).resolve().parent / "teacher")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None, help="把每个 patch 的全量结果写 JSONL")
    args = parser.parse_args()

    patches = analyse(args.wav, args.model_dir, args.top)
    if not patches:
        raise SystemExit("音频太短，凑不出一个 0.96 s patch")
    print(f"{args.wav}: {len(patches)} 个 patch（0.96 s / 步长 0.48 s）")
    for patch in patches:
        top = ", ".join(f"{item['class']} {item['score']:.2f}" for item in patch["top"])
        print(f"  {patch['center_s']:6.2f}s  {top}")
    print("\n目标类（每个 patch）")
    names = list(TARGET_CLASSES)
    print(f"  {'t(s)':>6} " + " ".join(f"{name:>10}" for name in names))
    for patch in patches:
        print(f"  {patch['center_s']:6.2f} " +
              " ".join(f"{patch['targets'][name]:10.3f}" for name in names))
    if args.json:
        with args.json.open("w", encoding="utf-8") as handle:
            for patch in patches:
                handle.write(json.dumps(patch, ensure_ascii=False) + "\n")
        print(f"\nJSONL -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
