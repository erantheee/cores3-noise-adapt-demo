"""Qianwen HTTP adapters. No SDK dependency; credentials never enter results."""
from __future__ import annotations

import base64
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import wave

CHAT_BASE = "https://maas.qianwenaiapi.com/compatible-mode/v1"
ASR_BASE = "https://maas.qianwenaiapi.com/api/v1"
CHAT_MODEL = "qwen3.7-flash"
ASR_MODEL = "qwen-audio-3.0-asr-flash"


class ProviderHTTPError(RuntimeError):
    """供应商返回非 2xx；code 供调用方区分语义（例如 ASR 的 400 = 无人声）。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"千问 API HTTP {code}")
        self.code = int(code)


class AsrNoSpeech(RuntimeError):
    """服务端判定音频里没有可识别的人声。

    实测（2026-10-26 现场重放）：该网关对静音、键盘、房间噪声统一返回
    HTTP 400 + 空响应体 ``{}``；同一封装下真人语音（含 -52 dBFS 弱语音、
    SNR 0 dB 混噪、400 ms 短句）都正常 200。因此 400 对上位机的语义是
    "这段没人声"，不是"接口请求失败"。
    """


def post_json(url: str, payload: dict, key: str, timeout: float) -> dict:
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")
    if not url.startswith("https://"):
        raise ValueError("千问 API 必须使用 HTTPS")
    # Some macOS Python distributions have no bundled CA store. Respect explicit
    # overrides; otherwise use the system CA bundle, with verification enabled.
    cafile = None
    if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE"):
        if os.path.isfile("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
    request = urllib.request.Request(
        url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "X-DashScope-SSE": "disable"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context(cafile=cafile)
        ) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not copy provider error bodies (possibly containing request data).
        raise ProviderHTTPError(exc.code) from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"千问 API 请求失败：{str(exc).replace(key, '[REDACTED]')[:160]}") from None
    if not isinstance(result, dict):
        raise RuntimeError("千问 API 返回格式错误")
    if result.get("code") and result.get("code") not in (200, "200", "OK", "Success"):
        raise RuntimeError("千问 API 返回业务错误")
    return result


def chat(prompt: str, *, key: str, model: str = CHAT_MODEL,
         base_url: str = CHAT_BASE, max_tokens: int = 1024,
         timeout: float = 45, structured: bool = True) -> dict:
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "enable_thinking": False, "stream": False}
    if structured:
        payload["response_format"] = {"type": "json_object"}
    result = post_json(base_url.rstrip("/") + "/chat/completions", payload, key, timeout)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("千问 LLM 未返回 choices")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise RuntimeError("千问 LLM 输出未正常结束")
    message = choice.get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("千问 LLM 未返回正文")
    # Normalize only the transport. Existing decision validation stays in place.
    return {"model": result.get("model"), "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn"}


def transcribe(pcm: bytes, rate: int, *, key: str, model: str = ASR_MODEL,
               base_url: str = ASR_BASE, timeout: float = 30) -> str:
    if rate <= 0 or rate > 192000 or not pcm or len(pcm) % 2:
        raise ValueError("ASR 输入必须为有效的单声道 PCM16")
    if len(pcm) / (2 * rate) > 300 or len(pcm) + 44 > 10 * 1024 * 1024:
        raise ValueError("ASR 音频超过 5 分钟或 10 MB")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    data = "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    payload = {"model": model, "input": {"messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": data}}
    ]}]}, "parameters": {"format": "wav", "sample_rate": str(rate)}}
    try:
        result = post_json(base_url.rstrip("/") +
                           "/services/aigc/multimodal-generation/generation",
                           payload, key, timeout)
    except ProviderHTTPError as exc:
        if exc.code == 400:
            # 该网关对"无人声"统一回 400 空体（见 AsrNoSpeech 文档）。这不是接口故障，
            # 上位机不能显示成"转写请求失败"。
            raise AsrNoSpeech("服务端未检出人声") from None
        raise
    # Official docs and live responses use different nesting depths.
    node = result
    for _ in range(4):
        if isinstance(node.get("text"), str):
            return node["text"].strip()
        sentence = node.get("sentence")
        if isinstance(sentence, dict) and isinstance(sentence.get("text"), str):
            return sentence["text"].strip()
        node = node.get("output")
        if not isinstance(node, dict):
            break
    raise RuntimeError("千问 ASR 响应缺少转写文本")
