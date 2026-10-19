"""AST（AudioSet 预训练）在 Mac 上当教师：给真机 16 kHz 录音打 AudioSet 标签。

为什么换掉 YAMNet INT8：社区那个 mel 输入版在本机实测输出不可信（无关类别也全在
0.5 附近，TTS 语音只勉强把 Speech 顶到 0.75）。原因可能是它的 INT8 校准/图外 mel
口径对不上；教师只需要"离线、可信"，用 torch 的 AST 更简单也更稳。

AST 口径：16 kHz 单声道、10.24 s 一个窗口（1024 帧 log-mel，模型内部自带前端）、
527 个 AudioSet 类别、多标签 sigmoid。窗口按 5.12 s 步长滑，尾部补静音。

用法：
    .venv/bin/python experiments/sound-events/teacher_ast.py <wav> [--json out.jsonl]
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
WINDOW_S = 10.24
HOP_S = 5.12
MODEL_ID = "MIT/ast-finetuned-audioset-10-10-0.4593"

#: 目标类：按显示名匹配（AudioSet 的 527 类里有这些）。
TARGET_NAMES = {
    "typing": ["typing"],
    "computer_keyboard": ["computer keyboard"],
    "clickety": ["clickety-clack"],
    "wind": ["wind"],
    "wind_mic": ["wind noise"],
    "knock": ["knock"],
    "tap": ["tap"],
    "speech": ["speech"],
    "silence": ["silence"],
    "noise": ["noise"],
    "music": ["music"],
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
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def resolve_targets(id2label: dict[int, str]) -> dict[str, int]:
    resolved: dict[str, int] = {}
    lowered = {index: label.lower() for index, label in id2label.items()}
    for target, needles in TARGET_NAMES.items():
        for needle in needles:
            matches = [index for index, label in lowered.items() if needle in label]
            if len(matches) == 1:
                resolved[target] = matches[0]
                break
            if len(matches) > 1:
                # 多个匹配时取最短的标签（"Wind" 比 "Wind chime" 更可能）——记录在下面。
                matches.sort(key=lambda index: len(lowered[index]))
                resolved[target] = matches[0]
                break
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="AST 教师：AudioSet 标签")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--cache", type=Path, default=None)
    args = parser.parse_args()

    import torch
    from transformers import ASTFeatureExtractor, ASTForAudioClassification

    audio = load_wav_mono(args.wav)
    feature_extractor = ASTFeatureExtractor.from_pretrained(MODEL_ID, cache_dir=args.cache)
    model = ASTForAudioClassification.from_pretrained(MODEL_ID, cache_dir=args.cache)
    model.eval()
    id2label = {int(key): value for key, value in model.config.id2label.items()}
    targets = resolve_targets(id2label)
    print("目标类下标：", {name: (targets.get(name), id2label.get(targets.get(name, -1)))
                     for name in TARGET_NAMES})

    window = int(WINDOW_S * SAMPLE_RATE)
    hop = int(HOP_S * SAMPLE_RATE)
    if len(audio) < window:
        audio = np.pad(audio, (0, window - len(audio)))
    rows = []
    for start in range(0, max(1, len(audio) - window + 1), hop):
        chunk = audio[start:start + window]
        if len(chunk) < window:
            chunk = np.pad(chunk, (0, window - len(chunk)))
        inputs = feature_extractor(chunk, sampling_rate=SAMPLE_RATE, return_tensors="pt",
                                   padding="max_length", truncation=True)
        with torch.no_grad():
            logits = model(**inputs).logits[0]
            probs = torch.sigmoid(logits)
        order = torch.argsort(probs, descending=True)[:args.top]
        center_s = (start + window / 2.0) / SAMPLE_RATE
        row = {
            "start_s": round(start / SAMPLE_RATE, 2),
            "center_s": round(center_s, 2),
            "top": [{"class": id2label[int(i)], "score": round(float(probs[i]), 4)}
                    for i in order],
            "targets": {name: round(float(probs[index]), 4)
                        for name, index in targets.items()},
        }
        rows.append(row)
        top = ", ".join(f"{item['class']} {item['score']:.2f}" for item in row["top"])
        print(f"  {center_s:6.2f}s  {top}")

    if rows:
        names = list(rows[0]["targets"])
        print("\n目标类（每个 10.24 s 窗）")
        print(f"  {'t(s)':>6} " + " ".join(f"{name:>10}" for name in names))
        for row in rows:
            print(f"  {row['center_s']:6.2f} " +
                  " ".join(f"{row['targets'][name]:10.3f}" for name in names))
    if args.json:
        with args.json.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\nJSONL -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
