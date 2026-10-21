#!/usr/bin/env python3
"""Persistent local Qwen3-ASR worker for the CoreS3 host.

stdin/stdout are JSON lines. The model is loaded once; every request carries 16-bit mono PCM.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import wave
from pathlib import Path

import mlx_speech


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
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                wav_path = Path(handle.name)
            try:
                with wave.open(str(wav_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes(pcm)
                result = model.generate(
                    str(wav_path), language="Chinese", max_new_tokens=512
                )
                emit({
                    "type": "transcript",
                    "id": request_id,
                    "language": result.language,
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
