#!/usr/bin/env python3
"""Persistent local Qwen3-ASR worker for the CoreS3 host.

stdin/stdout are JSON lines. The model is loaded once; every request carries 16-bit mono PCM.

请求字段（只增键）：
  {"id": "...", "rate": 16000, "pcm_base64": "...", "max_new_tokens": 96, "context": ""}
`max_new_tokens` 由上位机按**有效语音时长**收紧（2026-10-21 D33）：旧版固定 512，
一段 15 s 噪声能让模型在重复循环里吐出满屏"就是，就是…"。默认值也按音频时长计算，
上限 448（模型默认），下限 24。
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import wave
from pathlib import Path

import mlx_speech

#: 模型默认的单次生成上限（与 mlx_speech 的 Qwen3ASRTranscriber 默认值一致）。
DEFAULT_MAX_NEW_TOKENS = 448
#: 按语音时长换算：中文约 4–6 字/秒，token 与字接近 1:1；留 2 倍余量。
TOKENS_PER_SECOND = 12
MIN_NEW_TOKENS = 24


def token_budget(seconds: float, requested: object = None) -> int:
    """根据语音时长给生成 token 预算；调用方可用 `max_new_tokens` 进一步收紧。"""
    if isinstance(requested, (int, float)) and int(requested) > 0:
        budget = int(requested)
    else:
        budget = int(max(MIN_NEW_TOKENS, seconds * TOKENS_PER_SECOND))
    return max(MIN_NEW_TOKENS, min(DEFAULT_MAX_NEW_TOKENS, budget))


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    try:
        model = mlx_speech.asr.load("qwen3-asr-1.7b")
    except Exception as exc:  # noqa: BLE001 - error must cross the worker boundary
        emit({"type": "error", "error": f"ASR 模型加载失败：{exc}"})
        return 1
    emit({"type": "ready", "model": "qwen3-asr-1.7b-int8-mlx"})

    for line in sys.stdin:
        try:
            request = json.loads(line)
            request_id = str(request["id"])
            rate = int(request.get("rate", 16000))
            pcm = base64.b64decode(request["pcm_base64"], validate=True)
            seconds = len(pcm) / 2 / max(rate, 1)
            max_tokens = token_budget(seconds, request.get("max_new_tokens"))
            context = str(request.get("context") or "")[:200]
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                wav_path = Path(handle.name)
            try:
                with wave.open(str(wav_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes(pcm)
                result = model.generate(
                    str(wav_path), language="Chinese",
                    max_new_tokens=max_tokens, context=context,
                )
                emit({
                    "type": "transcript",
                    "id": request_id,
                    "language": result.language,
                    "seconds": round(seconds, 2),
                    "max_new_tokens": max_tokens,
                    "text": result.text.strip(),
                })
            finally:
                wav_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            emit({"type": "transcript_error", "id": locals().get("request_id", ""),
                  "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
