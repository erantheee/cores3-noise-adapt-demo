"""上位机 LLM 连接状态。

这里只验证模型可用并公开真实状态；录音转写和会话账本尚未接通时，不生成虚假摘要。
"""

from __future__ import annotations

import json
import os
import base64
import math
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from collections import deque
from array import array
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]


class AssistantService:
    # 现场录音、会议扩声和电视人声可能让 S3 连续保持 LIKELY，等不到 3 秒 NONE。
    # 到达上限就滚动提交 ASR，避免音频只被 60 秒内存上限裁掉却始终没有转写。
    _MAX_CONTINUOUS_SEGMENT_S = 15.0
    def __init__(self, voiceprint_snapshot: Callable[[], dict] | None = None) -> None:
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
        self._lock = threading.Lock()
        self._state = "checking" if self.configured else "not_configured"
        self._error = ""
        self._task = "检查模型配置"
        self._transcript = "尚无转写"
        self._summary = "尚无可总结会话"
        self._llm_warning = ""
        self._today_sessions = 0
        self._recording = False
        self._telemetry: list[dict[str, object]] = []
        self._continuous_stream = False
        self._conversation_active = False
        self._conversation_started_at: float | None = None
        self._conversation_started_wall = ""
        self._last_conversation_at: float | None = None
        self._capture_started_wall = ""
        self._segment_history: deque[dict[str, object]] = deque(maxlen=8)
        self._audio_rate = 16000
        self._voiceprint_snapshot = voiceprint_snapshot
        self._stream_sample_cursor = 0
        self._session_start_sample = 0
        self._preroll: deque[tuple[int, bytes]] = deque(maxlen=50)  # about 1 second
        self._session_audio = bytearray()
        self._asr_process: subprocess.Popen[str] | None = None
        self._asr_ready = False
        self._asr_error = ""
        self._asr_model = "qwen3-asr-1.7b-int8-mlx"
        self._pending: dict[str, tuple[str, int, str]] = {}
        self._pending_events: dict[str, dict[str, object]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def start(self) -> None:
        self._start_asr()
        if self.configured:
            threading.Thread(target=self._probe, name="llm-probe", daemon=True).start()

    def _start_asr(self) -> None:
        python = ROOT / ".asr-venv" / "bin" / "python"
        worker = ROOT / "tools" / "qwen_asr_worker.py"
        if not python.exists() or not worker.exists():
            self._asr_error = "ASR 环境未安装"
            return
        try:
            self._asr_process = subprocess.Popen(
                [str(python), str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
        except OSError as exc:
            self._asr_error = f"ASR worker 启动失败：{exc}"
            return
        threading.Thread(target=self._read_asr, name="qwen-asr", daemon=True).start()

    def _read_asr(self) -> None:
        process = self._asr_process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = message.get("type")
            if kind == "ready":
                with self._lock:
                    self._asr_ready = True
                    self._asr_error = ""
                    self._asr_model = str(message.get("model") or self._asr_model)
            elif kind in ("transcript", "transcript_error"):
                request_id = str(message.get("id") or "")
                with self._lock:
                    pending = self._pending.pop(request_id, None)
                if pending is None:
                    continue
                event_id, segment_index, speaker = pending
                transcript = str(message.get("text") or "").strip()
                if kind == "transcript_error":
                    with self._lock:
                        self._asr_error = str(message.get("error") or "转写失败")
                launch = None
                with self._lock:
                    event = self._pending_events.get(event_id)
                    if event is None:
                        continue
                    results = event["results"]
                    assert isinstance(results, dict)
                    results[segment_index] = (speaker, transcript)
                    event["remaining"] = int(event["remaining"]) - 1
                    if int(event["remaining"]) == 0:
                        ordered = [results[index] for index in sorted(results)]
                        attributed = "\n".join(
                            f"{who}：{text}" for who, text in ordered if text
                        ) or "ASR 未识别到有效中文语音"
                        self._transcript = attributed
                        for item in self._segment_history:
                            if item.get("number") == event["number"]:
                                item["transcript"] = attributed
                                break
                        self._task = f"正在判断第 {event['number']} 段是否为对话"
                        launch = (
                            event["number"], event["seconds"], event["chunks"],
                            event["chunks_lost"], event["telemetry"], attributed,
                            ordered,
                        )
                        del self._pending_events[event_id]
                if launch is not None:
                    threading.Thread(
                        target=self._summarize_event, args=launch,
                        name="llm-after-speaker-asr", daemon=True,
                    ).start()
            elif kind == "error":
                with self._lock:
                    self._asr_error = str(message.get("error") or "ASR worker 错误")
                    self._asr_ready = False

    def _probe(self) -> None:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "只回复 OK"}],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if result.get("model") != self.model:
                raise RuntimeError(f"服务返回模型 {result.get('model') or '未知'}")
            new_state, error = "ready", ""
        except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
            new_state, error = "error", str(exc)
        with self._lock:
            self._state = new_state
            self._error = error
            if new_state != "ready":
                self._task = error or "检查模型配置"
            elif not self._recording:
                self._task = "等待 S3 候选人声"

    def recording_started(self, rate: int, seconds: int | None = None) -> None:
        with self._lock:
            self._recording = True
            self._capture_started_wall = datetime.now().astimezone().strftime("%H:%M:%S")
            self._continuous_stream = not seconds
            self._audio_rate = rate
            self._stream_sample_cursor = 0
            self._session_start_sample = 0
            self._preroll.clear()
            self._session_audio = bytearray()
            if self._continuous_stream:
                self._task = f"正在监听 S3 候选人声 · {rate} Hz"
                self._transcript = "音频流已连接；等待候选人声片段"
            else:
                self._telemetry = []
                self._task = f"正在接收 S3 对话录音 · {rate} Hz"
                self._transcript = "录音进行中；等待结束后进入转写"
                self._summary = "候选录音进行中，尚未判定为对话"

    def feed_audio(self, chunk) -> None:
        pcm = bytes(getattr(chunk, "pcm", b""))
        if not pcm:
            return
        with self._lock:
            self._audio_rate = int(getattr(chunk, "rate", self._audio_rate))
            start_sample = self._stream_sample_cursor
            self._stream_sample_cursor += len(pcm) // 2
            if self._continuous_stream:
                if self._conversation_active:
                    self._session_audio.extend(pcm)
                else:
                    self._preroll.append((start_sample, pcm))
            elif self._recording:
                if not self._session_audio:
                    self._session_start_sample = start_sample
                self._session_audio.extend(pcm)
            # Bound one session to 60 seconds so a broken boundary cannot exhaust memory.
            max_bytes = self._audio_rate * 2 * 60
            if len(self._session_audio) > max_bytes:
                del self._session_audio[:-max_bytes]

    def observe_frame(self, frame) -> None:
        launch = None
        now = time.monotonic()
        with self._lock:
            if not self._recording:
                return
            conversation = str(getattr(frame, "conversation", None) or "NONE")
            has_conversation = conversation not in ("NONE", "none", "UNKNOWN")
            if self._continuous_stream and has_conversation and not self._conversation_active:
                self._conversation_active = True
                self._conversation_started_at = now
                self._conversation_started_wall = datetime.now().astimezone().strftime("%H:%M:%S")
                self._last_conversation_at = now
                self._telemetry = []
                preroll = list(self._preroll)
                self._session_start_sample = preroll[0][0] if preroll else self._stream_sample_cursor
                self._session_audio = bytearray().join(pcm for _, pcm in preroll)
                self._task = "检测到候选人声，正在记录并等待声纹归属"
                self._summary = "候选片段进行中，尚未判定为对话"
            if self._continuous_stream and not self._conversation_active:
                return
            item = {
                "environment": getattr(frame, "environment", None),
                "conversation": conversation,
                "voice_likelihood": round(float(getattr(frame, "voice_likelihood", 0.0)), 3),
                "ambient_open": getattr(frame, "ambient_open", None),
            }
            self._telemetry.append(item)
            self._telemetry = self._telemetry[-600:]
            if has_conversation:
                self._last_conversation_at = now
            ended_by_silence = bool(
                not has_conversation
                and self._last_conversation_at is not None
                and now - self._last_conversation_at >= 3.0
            )
            segment_duration = now - (self._conversation_started_at or now)
            reached_segment_limit = segment_duration >= self._MAX_CONTINUOUS_SEGMENT_S
            if self._continuous_stream and (ended_by_silence or reached_segment_limit):
                self._conversation_active = False
                self._today_sessions += 1
                number = self._today_sessions
                duration = segment_duration
                telemetry = list(self._telemetry)
                self._task = f"正在分析第 {number} 段候选人声"
                self._transcript = "正在按声纹窗口切分说话人…"
                self._summary = "尚未判定为对话"
                audio = bytes(self._session_audio)
                start_sample = self._session_start_sample
                self._session_audio = bytearray()
                ended_wall = datetime.now().astimezone().strftime("%H:%M:%S")
                self._segment_history.appendleft({
                    "number": number,
                    "started_at": self._conversation_started_wall,
                    "ended_at": ended_wall,
                    "duration_s": round(duration, 1),
                    "end_reason": "达到15秒，自动分段" if reached_segment_limit else "连续静音3秒",
                    "speaker": "正在进行声纹对齐",
                    "transcript": "正在转写",
                })
                launch = (number, duration, len(telemetry), 0, telemetry, audio,
                          self._audio_rate, start_sample)
        if launch is not None:
            self._dispatch_event(*launch)

    def recording_finished(self, *, audio_seconds: float, chunks: int, chunks_lost: int) -> None:
        with self._lock:
            self._recording = False
            if self._continuous_stream:
                self._continuous_stream = False
                return
            self._today_sessions += 1
            session_number = self._today_sessions
            telemetry = list(self._telemetry)
            audio = bytes(self._session_audio)
            self._session_audio = bytearray()
            self._task = f"正在分析第 {session_number} 段候选录音"
            self._transcript = "正在按声纹窗口切分说话人…"
            self._summary = "尚未判定为对话"
            start_sample = self._session_start_sample
            self._segment_history.appendleft({
                "number": session_number,
                "started_at": self._capture_started_wall,
                "ended_at": datetime.now().astimezone().strftime("%H:%M:%S"),
                "duration_s": round(audio_seconds, 1),
                "end_reason": "设备录音结束",
                "speaker": "正在进行声纹对齐",
                "transcript": "正在转写",
            })
        self._dispatch_event(session_number, audio_seconds, chunks, chunks_lost,
                             telemetry, audio, self._audio_rate, start_sample)

    def _dispatch_event(self, number: int, seconds: float, chunks: int,
                        chunks_lost: int, telemetry: list[dict[str, object]],
                        audio: bytes, rate: int, start_sample: int = 0) -> None:
        process = self._asr_process
        if self._asr_ready and process is not None and process.stdin is not None and audio:
            segments = self._speaker_segments(audio, rate, start_sample)
            labels = sorted({speaker for speaker, _ in segments})
            with self._lock:
                for item in self._segment_history:
                    if item.get("number") == number:
                        item["speaker"] = " / ".join(labels) if labels else "unknown"
                        break
            event_id = f"{number}-{time.monotonic_ns()}"
            with self._lock:
                self._pending_events[event_id] = {
                    "number": number, "seconds": seconds, "chunks": chunks,
                    "chunks_lost": chunks_lost, "telemetry": telemetry,
                    "remaining": len(segments), "results": {},
                }
                self._task = f"Qwen ASR 正在逐轮转写第 {number} 段候选人声"
                self._transcript = "正在本地转写并对齐 self / other / unknown…"
            try:
                for index, (speaker, segment_audio) in enumerate(segments):
                    request_id = f"{event_id}-{index}"
                    segment_audio, asr_gain_db = self._prepare_asr_audio(segment_audio)
                    with self._lock:
                        self._pending[request_id] = (event_id, index, speaker)
                        for item in self._segment_history:
                            if item.get("number") == number:
                                item["asr_gain_db"] = max(
                                    float(item.get("asr_gain_db", 0.0)), asr_gain_db
                                )
                                break
                    payload = {"id": request_id, "rate": rate,
                               "pcm_base64": base64.b64encode(segment_audio).decode("ascii")}
                    process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
                return
            except OSError as exc:
                with self._lock:
                    self._asr_error = f"写入 ASR worker 失败：{exc}"
        threading.Thread(
            target=self._summarize_event,
            args=(number, seconds, chunks, chunks_lost, telemetry, "", []),
            name="llm-session-summary", daemon=True,
        ).start()

    @staticmethod
    def _prepare_asr_audio(pcm: bytes) -> tuple[bytes, float]:
        """把远场 PCM 副本提升到约 -20 dBFS，最多 +18 dB；不改原始采集或声纹流。"""
        if len(pcm) < 2:
            return pcm, 0.0
        samples = array("h")
        samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(float(value) * value for value in samples) / max(len(samples), 1))
        target_rms = 32768.0 * 10.0 ** (-20.0 / 20.0)
        if rms < 1.0 or rms >= target_rms:
            return pcm, 0.0
        gain = min(10.0 ** (18.0 / 20.0), target_rms / rms)
        for index, value in enumerate(samples):
            samples[index] = max(-32768, min(32767, round(value * gain)))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes(), round(20.0 * math.log10(gain), 1)

    def _speaker_segments(self, audio: bytes, rate: int,
                          start_sample: int) -> list[tuple[str, bytes]]:
        """按声纹窗口切分候选音频；冲突、缺失和旧结果全部归 unknown。"""
        snapshot = {}
        if self._voiceprint_snapshot is not None:
            try:
                snapshot = self._voiceprint_snapshot() or {}
            except Exception:  # noqa: BLE001 - 身份不可用时必须退回 unknown
                snapshot = {}
        windows = snapshot.get("history") if snapshot.get("ready") else []
        windows = windows if isinstance(windows, list) else []
        total_samples = len(audio) // 2
        slice_samples = max(1, int(rate * 0.75))
        slices: list[tuple[str, bytes]] = []
        for offset in range(0, total_samples, slice_samples):
            end = min(total_samples, offset + slice_samples)
            midpoint = start_sample + (offset + end) // 2
            labels = {
                str(window.get("verdict"))
                for window in windows
                if isinstance(window, dict)
                and int(window.get("start_sample", -1)) <= midpoint
                < int(window.get("end_sample", -1))
                and window.get("verdict") in ("self", "other")
            }
            # Two overlapping windows disagreeing means the boundary is unresolved.
            label = next(iter(labels)) if len(labels) == 1 else "unknown"
            part = audio[offset * 2:end * 2]
            if slices and slices[-1][0] == label:
                slices[-1] = (label, slices[-1][1] + part)
            else:
                slices.append((label, part))
        # Very short turns are unreliable for both WavLM and ASR; retain their audio but
        # remove the identity claim by folding them into unknown.
        minimum = int(rate * 0.45) * 2
        normalized = [
            (speaker if len(pcm) >= minimum else "unknown", pcm)
            for speaker, pcm in slices if pcm
        ]
        merged: list[tuple[str, bytes]] = []
        for speaker, pcm in normalized:
            if merged and merged[-1][0] == speaker:
                merged[-1] = (speaker, merged[-1][1] + pcm)
            else:
                merged.append((speaker, pcm))
        return merged or [("unknown", audio)]

    def _summarize_event(self, number: int, seconds: float, chunks: int,
                         chunks_lost: int, telemetry: list[dict[str, object]],
                         transcript: str = "",
                         speaker_turns: list[tuple[str, str]] | None = None) -> None:
        if not self.configured:
            return
        conversations: dict[str, int] = {}
        environments: dict[str, int] = {}
        opened = 0
        voice_values: list[float] = []
        for item in telemetry:
            conv = str(item.get("conversation") or "UNKNOWN")
            env = str(item.get("environment") or "UNKNOWN")
            conversations[conv] = conversations.get(conv, 0) + 1
            environments[env] = environments.get(env, 0) + 1
            opened += int(item.get("ambient_open") is True)
            voice_values.append(float(item.get("voice_likelihood") or 0.0))
        facts = {
            "event": "s3_recording_finished",
            "session": number,
            "time": datetime.now().isoformat(timespec="seconds"),
            "audio_seconds": round(seconds, 2),
            "chunks": chunks,
            "chunks_lost": chunks_lost,
            "frames": len(telemetry),
            "conversation_states": conversations,
            "environments": environments,
            "s3_ambient_open_intent_frames": opened,
            "mean_voice_likelihood": round(sum(voice_values) / len(voice_values), 3)
            if voice_values else None,
            "transcript_available": bool(transcript),
            "transcript": transcript,
            "speaker_turns": speaker_turns or [],
        }
        fallback = self._local_summary(facts)
        prompt = (
            "你是耳机对话判定器。输入是按声纹窗口切分后的候选人声，不保证是对话。"
            "self=已注册佩戴者；other=只能证明非佩戴者，不能证明是同一个人；unknown=身份不确定。"
            "先判断为 对话/非对话/不确定。只有 self 与 other 出现有语义关联的轮流回应，"
            "才可判为对话；自言自语、哼唱、媒体人声、只有单方或身份不足都不能判为对话。"
            "输出不超过120字，必须以“判定：对话/非对话/不确定”开头，并说明依据。"
            "开放意图不等于 Mac 执行端真的开放，禁止把意图写成执行事实。"
            "没有逐字稿或声纹轮次时禁止猜测。数据："
            + json.dumps(facts, ensure_ascii=False)
        )
        payload = json.dumps({
            "model": self.model,
            # Opus 5.5 的中转实现先返回 thinking；实测 512 tokens 仍在正文前耗尽。
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages", payload,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
            summary = "".join(
                item.get("text", "") for item in result.get("content", [])
                if item.get("type") == "text"
            ).strip()
            if not summary:
                stop_reason = result.get("stop_reason") or "未知结束原因"
                content_types = ",".join(
                    str(item.get("type", "unknown")) for item in result.get("content", [])
                ) or "空内容"
                raise RuntimeError(f"模型未返回正文（{stop_reason}；{content_types}）")
            with self._lock:
                self._summary = summary
                self._llm_warning = ""
                self._task = "等待下一段 S3 候选人声"
        except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
            with self._lock:
                # ASR 与事件账本已经成功，LLM 拒答/超时不应让整个会话显示为失败。
                # 本地摘要只重述可验证字段，不推测谈话含义或执行端状态。
                self._summary = fallback
                self._llm_warning = f"Opus 未生成正文，已使用本地摘要：{exc}"
                self._task = "等待下一段 S3 候选人声"

    @staticmethod
    def _local_summary(facts: dict[str, object]) -> str:
        number = facts.get("session", "—")
        seconds = facts.get("audio_seconds", 0)
        lost = int(facts.get("chunks_lost", 0) or 0)
        intent = int(facts.get("s3_ambient_open_intent_frames", 0) or 0)
        transcript = str(facts.get("transcript") or "").strip()
        turns = facts.get("speaker_turns") or []
        labels = {str(turn[0]) for turn in turns if isinstance(turn, (list, tuple)) and turn}
        quality = "无丢块" if lost == 0 else f"丢失{lost}块"
        heard = f"；ASR：{transcript[:36]}" if transcript else "；ASR未识别到有效语音"
        if "self" in labels and "other" in labels:
            verdict = "不确定（检测到双方声纹，仍需语义确认）"
        elif labels == {"self"}:
            verdict = "非对话候选（仅检测到佩戴者）"
        elif labels == {"other"}:
            verdict = "不确定（仅检测到非佩戴者）"
        else:
            verdict = "不确定（声纹不足或冲突）"
        return (
            f"判定：{verdict}。候选片段{number}录音{seconds}秒，{quality}{heard}；"
            f"S3产生{intent}帧环境声开放意图，供门控复盘（不代表实际开放）。"
        )

    def snapshot(self) -> dict[str, object]:
        voiceprint = {}
        if self._voiceprint_snapshot is not None:
            try:
                voiceprint = self._voiceprint_snapshot() or {}
            except Exception:  # noqa: BLE001 - 观测字段失败不能拖垮主状态
                voiceprint = {}
        with self._lock:
            state, error = self._state, self._error
            candidate_duration = (
                round(time.monotonic() - self._conversation_started_at, 1)
                if self._conversation_active and self._conversation_started_at is not None else 0.0
            )
            segments = [dict(item) for item in self._segment_history]
        texts = {
            "not_configured": "未配置",
            "checking": f"正在连接 · {self.model}",
            "ready": f"已连接 · {self.model}",
            "error": "连接失败",
        }
        return {
            "available": state == "ready",
            "state": state,
            "state_text": texts[state],
            "task": self._task,
            "transcript": self._transcript,
            "summary_state": self._summary,
            "today_sessions": self._today_sessions,
            "model": self.model,
            "error": error,
            "asr_ready": self._asr_ready,
            "asr_model": self._asr_model,
            "asr_error": self._asr_error,
            "llm_warning": self._llm_warning,
            "capture_channel": {
                "active": self._recording,
                "mode": "持续采集" if self._continuous_stream else "定时录音",
                "started_at": self._capture_started_wall,
                "audio_seconds": round(self._stream_sample_cursor / max(self._audio_rate, 1), 1),
            },
            "candidate": {
                "active": self._conversation_active,
                "started_at": self._conversation_started_wall if self._conversation_active else "",
                "duration_s": candidate_duration,
                "speaker": str(voiceprint.get("verdict") or "unknown"),
                "speaker_score": voiceprint.get("score"),
            },
            "segments": segments,
        }
