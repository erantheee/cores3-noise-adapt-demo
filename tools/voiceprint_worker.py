#!/usr/bin/env python3
"""WavLM 声纹影子门控 worker（Mac/PC 侧，不进 S3）。

由上位机 `tools/host/voiceprint.py` 以子进程方式拉起：stdin 收 16 kHz PCM 的 JSON 行，
stdout 回 JSON 行（ready / verdict / error）。使用仓库 `.venv`（需要 torch + transformers），
上位机本身不需要装这些依赖。

协议：
  stdin  (每行): {"rate":16000,"pcm_base64":"..."}
  stdout (每行): {"type":"ready","clips":4,"dim":512,"threshold":0.8}
                 {"type":"verdict","verdict":"self"|"other"|"unknown","score":0.93,...}
                 {"type":"error","error":"..."}

判定：1.5 s 窗、0.75 s 步进；embedding 与佩戴者注册原型（设备录音的均值）
做 cosine。当前窗口达到 self 阈值即否决为本人；只有有声且明显低于阈值才判 other，
静音、边界区和不可靠窗口为 unknown。结果回传 S3，用双方轮次判断对话。
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


def normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0.0 else vector


def load_learned(path: Path, dim: int) -> tuple[np.ndarray | None, int]:
    """加载持续积累的原型（在线学习结果）；形状不符就当没有。"""
    if not path.exists():
        return None, 0
    try:
        data = np.load(path)
        prototype = np.asarray(data["prototype"], dtype=np.float64).ravel()
        updates = int(data["updates"])
    except (OSError, KeyError, ValueError):
        return None, 0
    if prototype.shape[0] != dim or updates <= 0:
        return None, 0
    return normalize(prototype), updates


def save_learned(path: Path, prototype: np.ndarray, updates: int) -> None:
    """原子落盘：学到了新原型就持久化，重启后继续用（不污染原始注册集）。"""
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, prototype=prototype, updates=updates)
    tmp.replace(path)


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
    parser.add_argument("--speech-floor-dbfs", type=float, default=-45.0)
    # 耳语级（2026-10-23 用户口径）：耳语词间停顿多、端侧 event/vl 对耳语断续，
    # 20 ms chunk 粒度下 speech_ratio 天然只有 0.15–0.3。旧硬编码门 0.35 会把
    # "WavLM 已打 0.90 的耳语窗"挡成 unknown。降到 0.15：仍要求窗内有真实语音
    # 活动（防纯噪声窗），但不再系统性排除耳语。身份判定仍由 score ≥ threshold 决定。
    parser.add_argument("--min-speech-ratio", type=float, default=0.15,
                        help="窗口内语音活动占比门槛（低于则 unknown，默认 0.15）")
    parser.add_argument("--unknown-margin", type=float, default=0.15)
    # 持续积累（2026-10-23 用户要求）：高置信 self 窗口在线更新声纹原型。
    # 只学"干净的高分样本"，混合说话（两人同时）分数在中间区，不会污染原型。
    parser.add_argument("--no-learn", action="store_true",
                        help="关闭在线声纹积累（默认开）")
    parser.add_argument("--learn-weight", type=float, default=0.08,
                        help="原型向新样本靠拢的权重（默认 0.08）")
    parser.add_argument("--learn-min-score", type=float, default=0.80,
                        help="只有 score ≥ 该值才学习（默认等于 self 阈值；"
                             "混合/中间区样本仍不会学）")
    parser.add_argument("--learn-min-dbfs", type=float, default=-40.0,
                        help="只有窗口 dBFS ≥ 该值才学习（太弱/远场不学）")
    parser.add_argument("--learn-min-speech-ratio", type=float, default=0.5,
                        help="只有语音活动占比 ≥ 该值才学习")
    args = parser.parse_args()

    emit({"type": "loading"})
    embedder = WavLMEmbedder()
    prototype, clip_count = enrollment_prototype(args.enroll_dir, embedder)
    learn_path = args.enroll_dir / "learned-prototype.npz"
    learn_updates = 0
    learned: np.ndarray | None = None
    if not args.no_learn:
        learned, learn_updates = load_learned(learn_path, int(prototype.shape[0]))
        if learned is not None:
            prototype = learned
    emit({"type": "ready", "clips": clip_count, "dim": int(prototype.shape[0]),
          "threshold": args.threshold, "learn_updates": learn_updates,
          "learned_from": str(learn_path.name) if learned is not None else ""})

    window = int(args.window_s * 16000)
    hop = int(args.hop_s * 16000)
    buffer = np.zeros(0, dtype=np.float32)
    activity_buffer = np.zeros(0, dtype=np.uint8)
    buffer_start_sample = 0
    smoothed: float | None = None
    peak_scores: list[float] = []
    windows = 0
    last_emit = 0.0
    last_save = 0.0
    learn_threshold = max(args.learn_min_score, args.threshold)

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
            speech_active = bool(payload.get("speech_active", False))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            emit({"type": "error", "error": f"输入协议错误：{exc}"})
            continue
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        buffer = np.concatenate([buffer, samples])
        activity_buffer = np.concatenate([
            activity_buffer, np.full(samples.shape[0], int(speech_active), dtype=np.uint8)
        ])
        # 处理所有完整窗；如果推理落后太多，丢掉旧样本只保留最新两窗，避免无限积压。
        while buffer.shape[0] >= window:
            if buffer.shape[0] > window * 4:
                dropped = buffer.shape[0] - window * 2
                buffer = buffer[-window * 2:]
                activity_buffer = activity_buffer[-window * 2:]
                buffer_start_sample += dropped
                emit({"type": "dropped", "dropped_s": round(float(buffer.shape[0]) / 16000, 2)})
            chunk = buffer[:window]
            speech_ratio = float(np.mean(activity_buffer[:window]))
            start_sample = buffer_start_sample
            end_sample = start_sample + window
            buffer = buffer[hop:]
            activity_buffer = activity_buffer[hop:]
            buffer_start_sample += hop
            started = time.monotonic()
            vector = embedder(chunk)
            score = float(vector @ prototype)
            rms = float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64)))
            dbfs = 20.0 * np.log10(max(rms, 1e-8))
            smoothed = score if smoothed is None else (
                args.smooth * smoothed + (1.0 - args.smooth) * score)
            windows += 1
            now = time.monotonic()
            # 耳语级（2026-10-23）：耳语窗的语音密度随时间波动（词间停顿），单窗判定
            # 会把"条件好的峰值窗"丢掉。保留最近 ~3 s（4 个 hop）的窗口分数，判定用
            # 峰值：耳语 6 s 里只要有一两个窗语音密度够（实测 0.90），就判 self。
            # 峰值只进不出：新窗分数低时峰值自然滑出窗口，误判不会滞留。
            peak_scores.append(score)
            while len(peak_scores) > 4:
                peak_scores.pop(0)
            peak = max(peak_scores)
            if speech_ratio < args.min_speech_ratio or dbfs < args.speech_floor_dbfs:
                verdict = "unknown"
            elif peak >= args.threshold:
                verdict = "self"
            elif peak <= args.threshold - args.unknown_margin:
                verdict = "other"
            else:
                verdict = "unknown"
            # 在线积累：判定用的还是旧原型（score 在上方算好），只把干净的高分
            # self 样本并进原型；混合/中间区样本不学，避免把对方声纹吃进来。
            if (not args.no_learn
                    and score >= learn_threshold
                    and speech_ratio >= args.learn_min_speech_ratio
                    and dbfs >= args.learn_min_dbfs):
                prototype = normalize(
                    prototype * (1.0 - args.learn_weight) + vector * args.learn_weight)
                learn_updates += 1
                if learn_updates == 1 or learn_updates % 4 == 0 or now - last_save >= 30.0:
                    save_learned(learn_path, prototype, learn_updates)
                    last_save = now
            if windows == 1 or now - last_emit >= 0.25:
                last_emit = now
                emit({"type": "verdict", "verdict": verdict,
                      "score": round(score, 4),
                      "smoothed": round(float(smoothed), 4),
                      "dbfs": round(float(dbfs), 1),
                      "speech_ratio": round(speech_ratio, 3),
                      "windows": windows,
                      "learn_updates": learn_updates,
                      "start_sample": start_sample,
                      "end_sample": end_sample,
                      "latency_ms": round((now - started) * 1000.0, 1)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
