#!/usr/bin/env python3
"""Cloud ASR worker retaining the local worker's JSON-lines protocol."""
import base64
import json
import os
import sys

from host.qianwen_api import ASR_BASE, ASR_MODEL, AsrNoSpeech, transcribe


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main():
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    model = os.environ.get("QIANWEN_ASR_MODEL", ASR_MODEL)
    base = os.environ.get("QIANWEN_ASR_BASE_URL", ASR_BASE)
    if not key:
        emit({"type": "error", "error": "未配置 DASHSCOPE_API_KEY"})
        return 1
    # ready means worker can accept jobs, not that remote inference was tested.
    emit({"type": "ready", "model": model})
    for line in sys.stdin:
        request_id = ""
        try:
            request = json.loads(line)
            request_id = str(request["id"])
            rate = int(request.get("rate", 16000))
            pcm = base64.b64decode(request["pcm_base64"], validate=True)
            text = transcribe(pcm, rate, key=key, model=model, base_url=base)
            emit({"type": "transcript", "id": request_id, "language": "",
                  "seconds": round(len(pcm) / 2 / rate, 2), "text": text})
        except AsrNoSpeech:
            # 服务端判"没人声"不是错误：按空转写回传，让上位机走"这段没听出内容"，
            # 而不是把整轮显示成"转写请求失败"。
            emit({"type": "transcript_nospeech", "id": request_id,
                  "seconds": round(len(pcm) / 2 / max(rate, 1), 2)})
        except Exception as exc:
            emit({"type": "transcript_error", "id": request_id,
                  "error": str(exc).replace(key, "[REDACTED]")[:200]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
