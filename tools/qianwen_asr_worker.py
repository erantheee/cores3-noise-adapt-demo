#!/usr/bin/env python3
"""Cloud ASR worker retaining the local worker's JSON-lines protocol."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from host.qianwen_api import ASR_BASE, ASR_MODEL, AsrNoSpeech, transcribe


ROOT = Path(__file__).resolve().parents[1]
LOCAL_FIRST_SPEECH_SECONDS = 3.0


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


class LocalFallback:
    """Lazy persistent local ASR used when short speech is unreliable in cloud.

    The child keeps the MLX model loaded after the first request.  It is deliberately
    isolated from the host interpreter because mlx_speech lives in ``.asr-venv``.
    """

    def __init__(self, python=None, worker=None):
        self.python = Path(python or ROOT / ".asr-venv" / "bin" / "python")
        self.worker = Path(worker or ROOT / "tools" / "qwen_asr_worker.py")
        self.process = None
        self.error = ""

    @property
    def available(self):
        return self.python.exists() and self.worker.exists()

    def _start(self):
        if self.process is not None:
            return True
        if not self.available:
            self.error = "本地 ASR 环境未安装"
            return False
        try:
            self.process = subprocess.Popen(
                [str(self.python), str(self.worker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.error = f"本地 ASR 启动失败：{exc}"
            return False
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "ready":
                return True
            if message.get("type") == "error":
                self.error = str(message.get("error") or "本地 ASR 启动失败")
                self.stop()
                return False
        self.error = "本地 ASR worker 启动后退出"
        self.stop()
        return False

    def transcribe(self, request):
        if not self._start() or self.process is None:
            return None
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        local_id = "local-" + str(request.get("id") or "")
        payload = dict(request)
        payload["id"] = local_id
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(message.get("id") or "") != local_id:
                    continue
                if message.get("type") == "transcript":
                    return str(message.get("text") or "").strip()
                self.error = str(message.get("error") or "本地 ASR 未返回正文")
                return None
        except OSError as exc:
            self.error = f"本地 ASR 通信失败：{exc}"
            self.stop()
        return None

    def stop(self):
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def prefer_local(request):
    """Route sparse/short speech locally before it can become a cloud HTTP 400."""
    try:
        speech_seconds = float(request.get("speech_seconds"))
    except (TypeError, ValueError):
        return False
    return 0.0 < speech_seconds < LOCAL_FIRST_SPEECH_SECONDS


def process_request(request, *, key, model, base, fallback):
    request_id = str(request["id"])
    rate = int(request.get("rate", 16000))
    pcm = base64.b64decode(request["pcm_base64"], validate=True)
    seconds = round(len(pcm) / 2 / max(rate, 1), 2)

    if fallback is not None and prefer_local(request):
        text = fallback.transcribe(request)
        if text:
            return {"type": "transcript", "id": request_id, "language": "",
                    "seconds": seconds, "text": text,
                    "backend": "local", "fallback_reason": "short_sparse"}

    try:
        text = transcribe(pcm, rate, key=key, model=model, base_url=base)
        return {"type": "transcript", "id": request_id, "language": "",
                "seconds": seconds, "text": text, "backend": "qianwen"}
    except AsrNoSpeech:
        if fallback is not None:
            text = fallback.transcribe(request)
            if text:
                return {"type": "transcript", "id": request_id, "language": "",
                        "seconds": seconds, "text": text,
                        "backend": "local", "fallback_reason": "cloud_nospeech"}
        return {"type": "transcript_nospeech", "id": request_id,
                "seconds": seconds, "backend": "qianwen"}


def main():
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    model = os.environ.get("QIANWEN_ASR_MODEL", ASR_MODEL)
    base = os.environ.get("QIANWEN_ASR_BASE_URL", ASR_BASE)
    if not key:
        emit({"type": "error", "error": "未配置 DASHSCOPE_API_KEY"})
        return 1
    fallback = (LocalFallback()
                if os.environ.get("QIANWEN_ASR_LOCAL_FALLBACK", "1") != "0"
                else None)
    # ready means worker can accept jobs, not that remote inference was tested.
    emit({"type": "ready", "model": model})
    try:
        for line in sys.stdin:
            request_id = ""
            try:
                request = json.loads(line)
                request_id = str(request["id"])
                emit(process_request(
                    request, key=key, model=model, base=base, fallback=fallback
                ))
            except Exception as exc:
                emit({"type": "transcript_error", "id": request_id,
                      "error": str(exc).replace(key, "[REDACTED]")[:200]})
    finally:
        if fallback is not None:
            fallback.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
