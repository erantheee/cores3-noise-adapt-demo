#!/usr/bin/env python3
"""WavLM 声纹影子门控 worker（Mac/PC 侧，不进 S3）。

由上位机 `tools/host/voiceprint.py` 以子进程方式拉起：stdin 收 16 kHz PCM 的 JSON 行，
stdout 回 JSON 行（ready / verdict / error）。使用仓库 `.venv`（需要 torch + transformers），
上位机本身不需要装这些依赖。

协议：
  stdin  (每行): {"rate":16000,"pcm_base64":"..."}
  stdout (每行): {"type":"ready","clips":4,"dim":512,"threshold":0.8}
                 {"type":"verdict","verdict":"self"|"other","score":0.93,"windows":7,"lag_ms":321}
                 {"type":"error","error":"..."}

判定：1.5 s 窗、0.75 s 步进；embedding 与注册原型（`记录/声纹/self/` 设备录音的均值）
做 cosine；EMA 平滑后大于阈值判 self，否则 other。**只做影子遥测，不参与决策。**
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: 需要 16-bit PCM")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        duration = samples.shape[0] / float(rate)
        target = int(round(duration * 16000))
        source_index = np.linspace(0.0, 1.0, num=samples.shape[0], endpoint=False)
        target_index = np.linspace(0.0, 1.0, num=target, endpoint=False)
        samples = np.interp(target_index, source_index, samples).astype(np.float32)
    return samples


class WavLMEmbedder:
    def __init__(self) -> None:
        from transformers import AutoFeatureExtractor, WavLMForXVector

        name = "microsoft/wavlm-base-plus-sv"
        self.feature_extractor = AutoFeatureExtractor.from_pretrained(name)
        self.model = WavLMForXVector.from_pretrained(name)
        self.model.eval()

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        import torch

        with torch.no_grad():
            inputs = self.feature_extractor(
                np.asarray(samples, dtype=np.float32), sampling_rate=16000,
                return_tensors="pt")
            vector = self.model(**inputs).embeddings.squeeze().cpu().numpy()
        vector = np.asarray(vector, dtype=np.float64).ravel()
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise RuntimeError("WavLM embedding 全零")
        return vector / norm


def enrollment_prototype(directory: Path, embedder: WavLMEmbedder) -> tuple[np.ndarray, int]:
    paths = sorted(directory.glob("*.wav"))
    if not paths:
        raise SystemExit(f"声纹注册目录没有 WAV：{directory}")
    vectors = []
    for path in paths:
        vectors.append(embedder(load_wav_mono(path)))
    prototype = np.mean(np.stack(vectors), axis=0)
    prototype /= np.linalg.norm(prototype)
    return prototype, len(vectors)


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enroll-dir", type=Path,
                        default=ROOT / "记录" / "声纹" / "self-device")
    parser.add_argument("--threshold", type=float, default=0.80)
    parser.add_argument("--window-s", type=float, default=1.5)
    parser.add_argument("--hop-s", type=float, default=0.75)
    parser.add_argument("--smooth", type=float, default=0.6)
    args = parser.parse_args()

    emit({"type": "loading"})
    embedder = WavLMEmbedder()
    prototype, clip_count = enrollment_prototype(args.enroll_dir, embedder)
    emit({"type": "ready", "clips": clip_count, "dim": int(prototype.shape[0]),
          "threshold": args.threshold})

    window = int(args.window_s * 16000)
    hop = int(args.hop_s * 16000)
    buffer = np.zeros(0, dtype=np.float32)
    smoothed: float | None = None
    windows = 0
    last_emit = 0.0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if int(payload.get("rate", 16000)) != 16000:
                emit({"type": "error", "error": f"不支持的采样率 {payload.get('rate')}"})
                continue
            pcm = base64.b64decode(payload["pcm_base64"])
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            emit({"type": "error", "error": f"输入协议错误：{exc}"})
            continue
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        buffer = np.concatenate([buffer, samples])
        # 处理所有完整窗；如果推理落后太多，丢掉旧样本只保留最新两窗，避免无限积压。
        while buffer.shape[0] >= window:
            if buffer.shape[0] > window * 4:
                buffer = buffer[-window * 2:]
                emit({"type": "dropped", "dropped_s": round(float(buffer.shape[0]) / 16000, 2)})
            chunk = buffer[:window]
            buffer = buffer[hop:]
            started = time.monotonic()
            vector = embedder(chunk)
            score = float(vector @ prototype)
            smoothed = score if smoothed is None else (
                args.smooth * smoothed + (1.0 - args.smooth) * score)
            windows += 1
            now = time.monotonic()
            verdict = "self" if smoothed > args.threshold else "other"
            if windows == 1 or now - last_emit >= 0.25:
                last_emit = now
                emit({"type": "verdict", "verdict": verdict,
                      "score": round(score, 4),
                      "smoothed": round(float(smoothed), 4),
                      "windows": windows,
                      "latency_ms": round((now - started) * 1000.0, 1)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
