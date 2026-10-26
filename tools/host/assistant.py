"""上位机 ASR/LLM 闭环：S3 候选人声 → 语音证据门控 → ASR（本地/千问云端） → 语义判定 → 环境声授权。

2026-10-21 现场反馈后的口径（D32/D33）：

1. **不把噪声送进 ASR。** 候选片段先过两道语音证据门：设备遥测（`voice_likelihood` 连续
   高于阈值、且不是键盘/风/撞击）与 20 ms 能量 VAD。两道门都找不到 ≥1 s 有效人声时，
   直接显示"未检测到有效人声"，不调用 ASR——上一版把 15 s 噪声放大 +17 dB 送进 Qwen3-ASR，
   模型会吐出一长串"就是，就是，就是…"的重复幻觉。
2. **ASR 输出要过幻觉过滤。** 即使有人声，入口音频变短后也可能出现循环重复；过滤规则会
   折叠重复片段，整段都是重复/语气词时判为无效，不再把噪声写成文字。
3. **LLM 判定是环境声执行的授权。** LLM（或本地确定性回退）对每段给出
   `dialogue: yes/no/unclear` 与 `ambient: open/close`；`__main__` 只在这份授权新鲜且
   `dialogue=yes` 时才允许执行端打开环境声。设备 `ambient_open` 仍是"意图"，不是执行事实。
4. **分段上限 6 s。** 15 s 一段会让判定在对话结束后才回来，LLM 与执行端看起来无关；
   6 s 一段能在对话进行中给出首次授权，授权 TTL 20 s 由后续片段刷新。

ASR 模型在多线程 worker 进程里常驻；LLM 调用在各自线程里，不阻塞串口读取。
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from .qianwen_api import ASR_MODEL, CHAT_BASE, CHAT_MODEL, chat

ROOT = Path(__file__).resolve().parents[2]

#: 只保留语气词/填充词时判为无效 ASR（噪声最常见的输出）。
_FILLER_CHARS = set("嗯啊哦噢喔哎唉诶呃额呀哈呵嘿嘛呢吧啦咯")
_FILLER_WORDS = {"嗯", "啊", "哦", "哎", "唉", "诶", "呃", "嗯嗯", "啊啊", "哦哦",
                 "就是", "那个", "然后", "这个", "但是", "所以", "其实"}
#: 真实短应答（不是语气词）。合并后的 1.5 s 轮次里"好/对/行"这类回答不能被当成噪声。
_SHORT_ANSWER_WORDS = {"好", "对", "行", "是", "收到", "可以", "好的", "对的", "行的",
                       "谢谢", "不用", "没事", "在", "有", "没有", "不", "要", "好嘞",
                       "嗯嗯", "哦哦", "对啊", "是的", "行吧", "好吧", "好呀", "可以呀",
                       "对的呀", "是啊", "在的", "稍等", "等一下"}


def _cjk_len(text: str) -> int:
    """参与"有效字数"统计的字符（去掉空白与标点）。"""
    return len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text))


def _merge_intervals(intervals: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    """合并重叠/间隔不超过 gap 的区间（单位：样本）。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _intervals_seconds(intervals: list[tuple[int, int]], rate: int) -> float:
    rate = max(int(rate), 1)
    return sum(max(0, end - start) for start, end in intervals) / rate


class AssistantService:
    #: 一段候选对话最长多长时间后滚动提交 ASR/LLM。15 s 太晚：判定回来时对话已经结束，
    #: 环境声授权与执行端看起来毫不相关（2026-10-21 现场反馈）。
    _MAX_CONTINUOUS_SEGMENT_S = 6.0
    #: LLM 判定有效多久。两个片段之间（6 s + ASR + LLM）必须留在窗口内，
    #: 但对话结束后设备意图会立刻关掉环境声，所以 20 s 只用于跨片段续期。
    _DECISION_TTL_S = 20.0
    #: 至少要有这么多秒的语音证据才送 ASR。低于它基本是噪声/呼吸/键盘。
    #: 2026-10-26 从 1.0 放到 0.55：一句短应答（"嗯，对""在吗"）能量证据常只有
    #: 0.6–0.9 s，1.0 会把真实短句整段跳过（现场 seg0376：0.84 s 被丢）。
    _MIN_SPEECH_S = 0.55
    #: 设备明确判为非人声的事件：这些帧覆盖的时段不再送云端 ASR。实测该网关对
    #: 键盘/风/异响统一回 HTTP 400（原始现场 400 段里 65% 带这些事件）。
    _BLOCKING_EVENTS = ("keyboard", "wind", "artifact")
    #: ASR 副本最长截取多少秒（正常片段 <= 6 s + 1 s 预缓冲，这里只是防御）。
    _MAX_ASR_SECONDS = 20.0
    #: 单轮转写最多显示多少字符；防止模型跑飞把面板刷屏。
    _MAX_TRANSCRIPT_CHARS = 240
    #: 15 s 音频最多允许 448 tokens（模型默认值）；按语音时长再收紧，避免跑飞。
    _MAX_ASR_TOKENS = 160

    def __init__(self, voiceprint_snapshot: Callable[[], dict] | None = None,
                 training=None,
                 memo_context: Callable[[], str] | None = None,
                 on_decision: Callable[[dict], None] | None = None) -> None:
        self.provider = os.environ.get("CORES3_LLM_PROVIDER", "anthropic")
        self.asr_provider = os.environ.get("CORES3_ASR_PROVIDER", "local")
        if self.provider not in ("anthropic", "qianwen"):
            raise ValueError("CORES3_LLM_PROVIDER 必须为 anthropic 或 qianwen")
        if self.asr_provider not in ("local", "qianwen"):
            raise ValueError("CORES3_ASR_PROVIDER 必须为 local 或 qianwen")
        if self.provider == "qianwen":
            self.base_url = os.environ.get("QIANWEN_LLM_BASE_URL", CHAT_BASE).rstrip("/")
            self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
            self.model = os.environ.get("QIANWEN_LLM_MODEL", CHAT_MODEL)
        else:
            self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            self.model = os.environ.get("ANTHROPIC_MODEL", "gpt-5.6-sol")
        # 重入锁：snapshot() 持锁时还会调用 ambient_authorization()。
        self._lock = threading.RLock()
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
        self._segment_history: deque[dict[str, object]] = deque(maxlen=24)
        self._audio_rate = 16000
        self._voiceprint_snapshot = voiceprint_snapshot
        self._stream_sample_cursor = 0
        self._session_start_sample = 0
        self._preroll: deque[tuple[int, bytes]] = deque(maxlen=50)  # about 1 second
        self._session_audio = bytearray()
        self._asr_process: subprocess.Popen[str] | None = None
        self._asr_ready = False
        self._asr_error = ""
        self._asr_model = (os.environ.get("QIANWEN_ASR_MODEL", ASR_MODEL)
                           if self.asr_provider == "qianwen" else "qwen3-asr-1.7b-int8-mlx")
        self._asr_write_lock = threading.Lock()
        self._pending: dict[str, tuple[str, int, str, float]] = {}
        self._pending_events: dict[str, dict[str, object]] = {}
        #: 环境声语义授权：最近一次 LLM/本地判定。只在 `dialogue=yes && ambient=open`
        #: 且未过期时允许执行端开放环境声（fail-closed）。
        self._decision: dict[str, object] | None = None
        self._decision_number = 0
        #: 已显示过的最新片段号：并发的 ASR 请求完成顺序可能乱序，旧片段不能覆盖新转写。
        self._transcript_number = 0
        #: 已显示过的最新摘要片段号：LLM 并发返回时旧摘要不能覆盖新摘要。
        self._summary_number = 0
        #: 环境声授权的"对话轮次"起点。6 s 滚动分段**不**推进它——否则每 6 s 就有
        #: 一个旧判定被判过期，执行端会开一下关一下。只有静音后真正开始新一轮对话才推进。
        self._authorization_epoch_started_at: float | None = None
        #: 判定与轮次绑定：stamp 在片段派发时，旧轮次/旧录音的判定不能给新轮次授权。
        self._authorization_epoch_id = 0
        self._new_episode_pending = True
        #: 训练数据落盘（tools/host/training.py 的鸭子接口）。None = 不采集。
        self._training = training
        #: 用户记忆（tools/host/memo.py）的注入与记录回调。None = 本次运行没有记忆。
        #: 记忆只作**弱先验**：prompt 里会写明与本段证据冲突时以本段为准。
        self._memo_context = memo_context
        self._on_decision = on_decision
        self._training_pending: dict[int, dict] = {}
        #: LLM 的跨片段上下文：同一轮对话里最近几轮转写 + 最近几次判定。
        #: 6 s 分段会把一来一回切到两个请求里，没有它 LLM 每段都是"单方发言"。
        self._llm_context: deque[dict[str, object]] = deque(maxlen=24)
        self._llm_context_verdicts: deque[dict[str, object]] = deque(maxlen=4)
        # 对话状态机（2026-10-26 用户口径）：LLM 维护"是不是还在对话里"，而不是每 6 s
        # 孤立判一次。yes 建立"对话进行中"；unclear 不打断进行中的对话；连续两次 no
        # 才确认"对话结束"（单次 no 常把"本段单向/无问答"误当结束，见 work/replay 复现）。
        self._dialogue_confirmed = False
        self._consecutive_no = 0
        #: ASR 吞吐/通过率遥测（2026-10-22）：让"只录到几句话"有数字可查。
        self._asr_stats: dict[str, int] = {
            "segments": 0, "passed": 0, "skipped": 0, "filtered": 0,
        }
        self._asr_latency_ms: deque[float] = deque(maxlen=50)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def start(self) -> None:
        self._start_asr()
        if self.configured:
            threading.Thread(target=self._probe, name="llm-probe", daemon=True).start()

    def _start_asr(self) -> None:
        python = (Path(sys.executable) if self.asr_provider == "qianwen"
                  else ROOT / ".asr-venv" / "bin" / "python")
        worker = ROOT / "tools" / ("qianwen_asr_worker.py"
                                   if self.asr_provider == "qianwen" else "qwen_asr_worker.py")
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
            elif kind in ("transcript", "transcript_error", "transcript_nospeech"):
                request_id = str(message.get("id") or "")
                with self._lock:
                    pending = self._pending.pop(request_id, None)
                if pending is None:
                    continue
                event_id, segment_index, speaker, turn_seconds, sent_at = pending
                asr_latency_ms = max(0.0, (time.monotonic() - sent_at) * 1000.0)
                with self._lock:
                    self._asr_latency_ms.append(asr_latency_ms)
                transcript = ("" if kind == "transcript_nospeech"
                              else str(message.get("text") or "").strip())
                if kind == "transcript_error":
                    with self._lock:
                        self._asr_error = str(message.get("error") or "转写失败")
                launch = None
                launch_asr_errors = []
                with self._lock:
                    event = self._pending_events.get(event_id)
                    if event is None:
                        continue
                    if kind == "transcript_error":
                        event.setdefault("asr_errors", []).append(
                            str(message.get("error") or "转写失败")[:200]
                        )
                    else:
                        self._asr_error = ""
                        if kind == "transcript_nospeech":
                            notes = event.setdefault("asr_notes", [])
                            assert isinstance(notes, list)
                            if "服务端未检出人声" not in notes:
                                notes.append("服务端未检出人声")
                    results = event["results"]
                    assert isinstance(results, dict)
                    raw_results = event.setdefault("raw_results", {})
                    assert isinstance(raw_results, dict)
                    raw_results[segment_index] = transcript[:120]
                    cleaned, note = self._filter_asr_text(
                        transcript, turn_seconds, speaker
                    )
                    if note:
                        notes = event.setdefault("asr_notes", [])
                        assert isinstance(notes, list)
                        if note not in notes:
                            notes.append(note)
                    results[segment_index] = (speaker, cleaned)
                    event["remaining"] = int(event["remaining"]) - 1
                    if int(event["remaining"]) == 0:
                        ordered = [results[index] for index in sorted(results)]
                        raw_ordered = [
                            str(raw_results.get(index) or "") for index in sorted(results)
                        ]
                        raw_text = " / ".join(item for item in raw_ordered if item)[:120]
                        had_raw = bool(raw_text)
                        attributed = "\n".join(
                            f"{who}：{text}" for who, text in ordered if text
                        )
                        launch_asr_errors = list(event.get("asr_errors") or [])
                        notes = [str(item) for item in event.get("asr_notes") or []]
                        notes.extend("ASR 请求失败：" + error for error in launch_asr_errors)
                        event.setdefault("asr_latency_ms", []).append(round(asr_latency_ms, 1))
                        if any(text for _, text in ordered):
                            self._asr_stats["passed"] += 1
                        else:
                            self._asr_stats["filtered"] += 1
                        if not attributed:
                            # 对用户不要说"噪声/重复/丢弃"（2026-10-22 用户反馈：这是
                            # 内部诊断语言，且比实际判定"不确定"下结论更重）。
                            attributed = ("本段转写请求失败（详情见 asr_note）" if launch_asr_errors
                                          else "这段没听出有效内容，未转写（详情见 asr_note）")
                        number = int(event["number"])
                        event_epoch = int(event.get("epoch_id", 0))
                        # 上一轮对话的 ASR 晚归：转写与摘要也不能覆盖当前轮次的状态。
                        if event_epoch != self._authorization_epoch_id:
                            del self._pending_events[event_id]
                            continue
                        if number >= self._transcript_number:
                            self._transcript_number = number
                            self._transcript = attributed
                        for item in self._segment_history:
                            if item.get("number") == number:
                                item["transcript"] = attributed
                                if raw_text:
                                    item["asr_raw"] = raw_text
                                note_text = "；".join(notes)
                                if note_text:
                                    item["asr_note"] = note_text
                                break
                        self._task = f"正在判断第 {number} 段是否为对话"
                        self._note_context_turns(number, event_epoch, ordered)
                        self._update_training(number, {
                            "asr": {
                                "turns": [{"speaker": who, "text": text}
                                          for who, text in ordered],
                                "raw_turns": [
                                    {"speaker": results[index][0],
                                     "raw": raw_ordered[index]}
                                    for index in sorted(results)
                                ],
                                "transcript": attributed,
                                "notes": notes,
                                "latency_ms": event.get("asr_latency_ms") or [],
                            },
                        })
                        launch = (
                            event["number"], event["seconds"], event["chunks"],
                            event["chunks_lost"], event["telemetry"], attributed,
                            ordered, event_epoch, had_raw, raw_text,
                            event.get("llm_context") or {},
                        )
                        del self._pending_events[event_id]
                if launch is not None and launch_asr_errors:
                    # A failed/missing turn is not negative speech evidence. Even
                    # partial transcripts must not authorize from incomplete turns.
                    number, epoch = int(launch[0]), int(launch[7])
                    reason = "ASR 请求失败，无法判断：" + launch_asr_errors[0]
                    self._publish_decision(number, "unclear", "close", 0.0,
                                           reason, "local", epoch_id=epoch)
                    # 只少部分片段失败但拿到了正文时，不要把整轮说成"转写请求失败"：
                    # 面板上已有转写内容，写清"部分片段失败"即可（2026-10-26 用户反馈）。
                    has_text = any(text for _, text in launch[6])
                    headline = ("判定：不确定 · 部分片段转写失败" if has_text
                                else "判定：不确定 · 转写请求失败")
                    self._set_summary(number, headline, epoch_id=epoch)
                    self._finish_training(number, {
                        "asr": {"errors": launch_asr_errors},
                        "llm": {"skipped": "ASR 请求失败或轮次不完整"},
                        "labels": {"teacher": {"dialogue": "unclear", "ambient": "close",
                                   "confidence": 0.0, "reason": reason, "source": "local"}},
                    })
                    with self._lock:
                        self._task = "等待下一段 S3 候选人声"
                    continue
                if launch is not None and any(text for _, text in launch[6]):
                    if self.configured:
                        threading.Thread(
                            target=self._summarize_event, args=launch,
                            name="llm-after-speaker-asr", daemon=True,
                        ).start()
                    else:
                        # 有转写但没有 LLM：不能伪造语义判定；环境声授权走"未配置"回退。
                        number = int(launch[0])
                        self._set_summary(
                            number,
                            self._local_summary({
                                "session": number, "audio_seconds": launch[1],
                                "chunks_lost": launch[3], "transcript": launch[5],
                                "speaker_turns": launch[6],
                                "s3_ambient_open_intent_frames": sum(
                                    1 for item in launch[4]
                                    if item.get("ambient_open") is True
                                ),
                            })
                            + "\nLLM 未配置，无法给出语义判定；环境声按回退门控处理。",
                            epoch_id=int(launch[7]),
                        )
                        with self._lock:
                            self._task = "等待下一段 S3 候选人声"
                        self._publish_decision(
                            number, "unclear", "close", 0.0,
                            "LLM 未配置，无法做语义判定", "local",
                            epoch_id=int(launch[7]),
                        )
                        self._finish_training(number, {
                            "llm": {"skipped": "未配置 LLM"},
                            "labels": {"teacher": {
                                "dialogue": "unclear", "ambient": "close",
                                "confidence": 0.0,
                                "reason": "LLM 未配置，无法做语义判定",
                                "source": "local",
                            }},
                        })
                elif launch is not None:
                    # ASR 没有可显示的语音。若模型其实听到了内容、只是被当噪声/语气词
                    # 过滤，不能冒充"确定非对话"——那会把真实的一句短回答判死。
                    number = int(launch[0])
                    had_raw = bool(launch[8])
                    raw_text = str(launch[9] or "")
                    if had_raw:
                        reason = "ASR 只输出语气词/重复，无法确认语义" + (
                            f"（原始：{raw_text[:60]}）" if raw_text else ""
                        )
                        summary = "判定：不确定 · 环境声：关闭 · 置信度 0.20\n依据：" + reason
                        self._publish_decision(
                            number, "unclear", "close", 0.2, reason, "local",
                            epoch_id=int(launch[7]),
                        )
                    else:
                        reason = "ASR 未得到有效语音（噪声/重复幻觉已丢弃）"
                        summary = "判定：非对话 · 环境声：关闭 · 置信度 0.90\n依据：" + reason
                        self._publish_decision(
                            number, "no", "close", 0.9, reason, "local",
                            epoch_id=int(launch[7]),
                        )
                    self._set_summary(number, summary, epoch_id=int(launch[7]))
                    with self._lock:
                        self._task = "等待下一段 S3 候选人声"
                    self._finish_training(number, {
                        "asr": {"raw_text": raw_text, "had_raw": had_raw,
                                 "filtered_out": True},
                        "labels": {"teacher": {
                            "dialogue": "unclear" if had_raw else "no",
                            "ambient": "close",
                            "confidence": 0.2 if had_raw else 0.9,
                            "reason": reason, "source": "local",
                        }},
                    })
            elif kind == "error":
                with self._lock:
                    self._asr_error = str(message.get("error") or "ASR worker 错误")
                    self._asr_ready = False
        # stdout 关闭 = worker 退出/崩溃。必须显式把"未就绪"写回状态并关闭未完成的
        # 事件，否则面板会永远停在"Qwen ASR 正在逐轮转写"。
        with self._lock:
            self._asr_ready = False
            if not self._asr_error:
                self._asr_error = "ASR worker 已退出"
            abandoned = [dict(event) for event in self._pending_events.values()]
            self._pending_events.clear()
            self._pending.clear()
        # D34：ASR/LLM 只是迟到否决，不是快路径的前置条件。ASR 死了就退回纯本地
        # 快路径；已经发布的语义否决仍保留到本轮结束（不静默撤销）。
        for event in abandoned:
            number = int(event.get("number", 0))
            self._finish_training(number, {
                "asr": {"error": "ASR worker 已退出"},
                "labels": {"teacher": None,
                           "note": "ASR worker 已退出，未得到教师标签"},
            })
        with self._lock:
            self._task = "ASR worker 已退出；环境声退回本地快路径（无 LLM 否决）"
            self._asr_error = self._asr_error or "ASR worker 已退出"

    def _set_summary(self, number: int, text: str, epoch_id: int | None = None) -> None:
        """摘要按片段号 + 对话轮次排序：旧轮次/旧片段的响应不能覆盖新状态。"""
        with self._lock:
            if epoch_id is not None and int(epoch_id) != self._authorization_epoch_id:
                return
            if number < self._summary_number:
                return
            self._summary_number = number
            self._summary = text

    def _request_llm(self, prompt: str, max_tokens: int = 1024,
                     timeout: float = 45, structured: bool = True) -> dict:
        if self.provider == "qianwen":
            return chat(prompt, key=self.api_key, model=self.model, base_url=self.base_url,
                        max_tokens=max_tokens, timeout=timeout, structured=structured)
        payload = json.dumps({"model": self.model, "max_tokens": max_tokens,
                              "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages", payload,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def _probe(self) -> None:
        try:
            result = self._request_llm("只回复 OK", max_tokens=32, timeout=30, structured=False)
            if result.get("model") != self.model:
                raise RuntimeError(f"服务返回模型 {result.get('model') or '未知'}")
            new_state, error = "ready", ""
        except (OSError, ValueError, RuntimeError, KeyError, AttributeError, TypeError,
                urllib.error.HTTPError) as exc:
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
            # 每次 capture 开始（含设备重连后补发 started）都是新一轮采集：任何旧判定
            # 都必须失效。启动时主机登记与设备 started 各调一次，此时本来就没有判定，
            # 清空是无害的。
            self._authorization_epoch_id += 1
            self._decision = None
            self._decision_number = 0
            self._llm_context.clear()
            self._llm_context_verdicts.clear()
            self._authorization_epoch_started_at = None
            self._new_episode_pending = True
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
                # 滚动分段（达到 6 s 上限）不是新一轮对话；只有静音后重新开口才推进
                # 环境声授权轮次，避免旧判定被每 6 s 判一次过期。达到上限后
                # `_conversation_active` 为 False 时不会走下面的静音分支，所以这里同时
                # 用"距上一帧对话证据的间隔 >=3 s"识别真正的重新开始。
                gap_since_last = (
                    now - self._last_conversation_at
                    if self._last_conversation_at is not None else None
                )
                new_episode = bool(
                    self._new_episode_pending
                    or self._authorization_epoch_started_at is None
                    or (gap_since_last is not None and gap_since_last >= 3.0)
                )
                if new_episode:
                    self._authorization_epoch_id += 1
                    self._authorization_epoch_started_at = now
                    self._new_episode_pending = False
                    self._llm_context.clear()
                    self._llm_context_verdicts.clear()
                    self._dialogue_confirmed = False
                    self._consecutive_no = 0
                self._conversation_started_wall = datetime.now().astimezone().strftime("%H:%M:%S")
                self._last_conversation_at = now
                self._telemetry = []
                preroll = list(self._preroll)
                self._session_start_sample = preroll[0][0] if preroll else self._stream_sample_cursor
                self._session_audio = bytearray().join(pcm for _, pcm in preroll)
                self._task = "检测到候选人声，正在记录并等待语音证据"
                self._summary = "候选片段进行中，尚未判定为对话"
            if self._continuous_stream and not self._conversation_active:
                return
            item = {
                "environment": getattr(frame, "environment", None),
                "conversation": conversation,
                "voice_likelihood": round(float(getattr(frame, "voice_likelihood", 0.0)), 3),
                "ambient_open": getattr(frame, "ambient_open", None),
                "event": getattr(frame, "event", None),
                # 帧到达时的音频游标：把设备语音证据映射回这次采集的 PCM。
                "sample": self._stream_sample_cursor,
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
                if ended_by_silence:
                    # 真正的对话结束：下一段重新开口时推进授权轮次。
                    self._new_episode_pending = True
                self._today_sessions += 1
                self._asr_stats["segments"] += 1
                number = self._today_sessions
                duration = segment_duration
                telemetry = list(self._telemetry)
                self._task = f"正在分析第 {number} 段候选人声"
                self._transcript = "正在收集语音证据并切分说话人…"
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
                    "end_reason": (
                        f"达到{self._MAX_CONTINUOUS_SEGMENT_S:g}秒，自动分段"
                        if reached_segment_limit else "连续静音3秒"
                    ),
                    "speaker": "正在进行声纹对齐",
                    "transcript": "正在收集语音证据",
                    "speech_s": None,
                    "asr_note": "",
                    "asr_raw": "",
                })
                launch = (number, duration, len(telemetry), 0, telemetry, audio,
                          self._audio_rate, start_sample, self._authorization_epoch_id)
        if launch is not None:
            self._spawn_dispatch(*launch)

    def recording_finished(self, *, audio_seconds: float, chunks: int, chunks_lost: int) -> None:
        with self._lock:
            self._recording = False
            if self._continuous_stream:
                self._continuous_stream = False
                return
            self._today_sessions += 1
            self._asr_stats["segments"] += 1
            session_number = self._today_sessions
            telemetry = list(self._telemetry)
            audio = bytes(self._session_audio)
            self._session_audio = bytearray()
            self._task = f"正在分析第 {session_number} 段候选录音"
            self._transcript = "正在收集语音证据并切分说话人…"
            self._summary = "尚未判定为对话"
            start_sample = self._session_start_sample
            self._segment_history.appendleft({
                "number": session_number,
                "started_at": self._capture_started_wall,
                "ended_at": datetime.now().astimezone().strftime("%H:%M:%S"),
                "duration_s": round(audio_seconds, 1),
                "end_reason": "设备录音结束",
                "speaker": "正在进行声纹对齐",
                "transcript": "正在收集语音证据",
                "speech_s": None,
                "asr_note": "",
                "asr_raw": "",
            })
        self._spawn_dispatch(session_number, audio_seconds, chunks, chunks_lost,
                             telemetry, audio, self._audio_rate, start_sample,
                             self._authorization_epoch_id)

    # ------------------------------------------------------------------ 分段→ASR

    def _spawn_dispatch(self, *args) -> None:
        """VAD/ASR 派发放在独立线程：串口线程不能被几百毫秒的 VAD 阻塞。"""
        threading.Thread(
            target=self._run_dispatch, args=args, name="asr-dispatch", daemon=True,
        ).start()

    # ------------------------------------------------------------------ 上下文/训练

    def _context_facts(self) -> dict[str, object]:
        with self._lock:
            turns = [dict(item) for item in self._llm_context]
            verdicts = [dict(item) for item in self._llm_context_verdicts]
        return {
            "turn_count": len(turns),
            "recent_turns": turns,
            "previous_verdicts": verdicts,
        }

    def _note_context_turns(self, number: int, epoch_id: int,
                            turns: list[tuple[str, str]]) -> None:
        with self._lock:
            if int(epoch_id) != self._authorization_epoch_id:
                return
            for speaker, text in turns:
                if not text and speaker != "self":
                    continue
                self._llm_context.append({
                    "segment": number,
                    "speaker": speaker,
                    # self 说了话但内容被过滤时也要留下轮次证据：LLM 判"无 self 回应"
                    # 是环境声门不开的主因（2026-10-22 事故复盘）。
                    "text": str(text)[:160] if text else "（短应答，内容未识别）",
                })

    def _update_training(self, number: int, extra: dict[str, object]) -> None:
        with self._lock:
            record = self._training_pending.get(number)
            if record is None:
                return
            record.update(extra)

    def _finish_training(self, number: int, extra: dict[str, object] | None = None) -> None:
        with self._lock:
            record = self._training_pending.pop(number, None)
        if record is None:
            return
        if extra:
            labels = extra.get("labels")
            record.update({key: value for key, value in extra.items() if key != "labels"})
            if isinstance(labels, dict):
                merged = dict(record.get("labels") or {})
                merged.update(labels)
                record["labels"] = merged
        training = self._training
        if training is not None:
            training.write_segment(record)

    def _begin_training(self, number: int, seconds: float, chunks: int,
                        chunks_lost: int, telemetry: list[dict[str, object]],
                        rate: int, audio: bytes, start_sample: int,
                        speech_s: float, evidence: str, epoch_id: int) -> None:
        if self._training is None:
            return
        with self._lock:
            segment = next(
                (dict(item) for item in self._segment_history if item.get("number") == number),
                {},
            )
        conversations: dict[str, int] = {}
        environments: dict[str, int] = {}
        voice_values: list[float] = []
        intent_frames = 0
        for item in telemetry:
            conversations[str(item.get("conversation") or "UNKNOWN")] = (
                conversations.get(str(item.get("conversation") or "UNKNOWN"), 0) + 1
            )
            environments[str(item.get("environment") or "UNKNOWN")] = (
                environments.get(str(item.get("environment") or "UNKNOWN"), 0) + 1
            )
            voice_values.append(float(item.get("voice_likelihood") or 0.0))
            intent_frames += int(item.get("ambient_open") is True)
        audio_path = None
        if audio:
            audio_path = self._training.write_audio(f"seg{number:04d}", audio, rate)
        record = {
            "schema": getattr(self._training, "SCHEMA", 1),
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "epoch": int(epoch_id),
            "segment": number,
            "started_at": segment.get("started_at"),
            "ended_at": segment.get("ended_at"),
            "duration_s": round(seconds, 2),
            "end_reason": segment.get("end_reason"),
            "audio": None if audio_path is None else {
                "path": audio_path,
                "rate": int(rate),
                "seconds": round(len(audio) / 2 / max(rate, 1), 2),
            },
            "device": {
                "speech_s": round(float(speech_s), 2),
                "evidence": evidence,
                "frames": len(telemetry),
                "conversation_states": conversations,
                "environments": environments,
                "mean_voice_likelihood": round(sum(voice_values) / len(voice_values), 3)
                if voice_values else None,
                "ambient_open_intent_frames": intent_frames,
                "telemetry": telemetry[-120:],
            },
            "llm_context": self._context_facts(),
            "labels": {"teacher": None, "human": None},
        }
        with self._lock:
            # 防御：ASR/LLM 卡死时避免训练记录无限堆积内存。
            if len(self._training_pending) >= 64:
                oldest = min(self._training_pending)
                stale = self._training_pending.pop(oldest)
                stale["note"] = "被后续片段挤出，未等到 ASR/LLM"
                self._training.write_segment(stale)
            self._training_pending[number] = record

    def _run_dispatch(self, *args) -> None:
        """隔离派发线程的异常：单段分析失败不能把面板永远停在"正在分析"。"""
        try:
            self._dispatch_event(*args)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须把失败交回状态
            number = int(args[0]) if args else 0
            epoch_id = int(args[-1]) if args and isinstance(args[-1], int) else None
            message = f"候选段分析失败：{str(exc)[:120]}"
            with self._lock:
                if number >= self._transcript_number:
                    self._transcript_number = number
                    self._transcript = message
                self._task = "等待下一段 S3 候选人声"
            self._set_summary(
                number, f"判定：不确定 · 环境声：关闭 · 置信度 0.0\n依据：{message}",
                epoch_id=epoch_id,
            )
            self._publish_decision(
                number, "unclear", "close", 0.0, message, "local", epoch_id=epoch_id
            )

    def _dispatch_event(self, number: int, seconds: float, chunks: int,
                        chunks_lost: int, telemetry: list[dict[str, object]],
                        audio: bytes, rate: int, start_sample: int = 0,
                        epoch_id: int | None = None) -> None:
        if epoch_id is None:
            # 直接调用（单测/老路径）没有传轮次号：用当前值。
            with self._lock:
                epoch_id = self._authorization_epoch_id
        else:
            epoch_id = int(epoch_id)
        llm_context = self._context_facts()
        regions, speech_s, evidence = self._select_speech_regions(
            audio, rate, telemetry, start_sample
        )
        self._begin_training(
            number, seconds, chunks, chunks_lost, telemetry, rate, audio,
            start_sample, speech_s, evidence, epoch_id,
        )
        with self._lock:
            for item in self._segment_history:
                if item.get("number") == number:
                    item["speech_s"] = round(speech_s, 2)
                    item["asr_note"] = evidence
                    break
        if speech_s < self._MIN_SPEECH_S:
            # 关键修复：没有人声证据就不送 ASR。旧版把 15 s 噪声放大后送进模型，
            # 输出会是一长串重复幻觉（现场截图里满屏的"就是"）。
            self._asr_stats["skipped"] += 1
            reason = f"未检测到有效人声（{evidence}），跳过 ASR"
            with self._lock:
                if number >= self._transcript_number:
                    self._transcript_number = number
                    self._transcript = f"第 {number} 段未检测到有效人声，已跳过转写"
                self._task = "等待下一段 S3 候选人声"
                for item in self._segment_history:
                    if item.get("number") == number:
                        item["transcript"] = "未检测到有效人声（跳过 ASR）"
                        item["asr_note"] = evidence
                        item["speaker"] = "未检测到人声"
                        break
            self._set_summary(
                number, f"判定：非对话 · 环境声：关闭 · 置信度 0.90\n依据：{reason}"
            )
            self._publish_decision(
                number, "no", "close", 0.9, reason, "local", epoch_id=epoch_id
            )
            self._finish_training(number, {
                "asr": {"skipped": "无语音证据"},
                "labels": {"teacher": {"dialogue": "no", "ambient": "close",
                                        "confidence": 0.9, "reason": reason,
                                        "source": "local"}},
            })
            return

        trimmed, trim_start = self._trim_to_speech(audio, rate, regions)
        if not trimmed:
            with self._lock:
                if number >= self._transcript_number:
                    self._transcript_number = number
                    self._transcript = f"第 {number} 段语音证据不足，已跳过转写"
                self._asr_stats["skipped"] += 1
                self._task = "等待下一段 S3 候选人声"
            self._publish_decision(
                number, "no", "close", 0.8, "语音证据不足，跳过 ASR", "local",
                epoch_id=epoch_id,
            )
            self._finish_training(number, {
                "asr": {"skipped": "语音证据不足"},
                "labels": {"teacher": {"dialogue": "no", "ambient": "close",
                                        "confidence": 0.8,
                                        "reason": "语音证据不足，跳过 ASR",
                                        "source": "local"}},
            })
            return

        process = self._asr_process
        if not (self._asr_ready and process is not None and process.stdin is not None):
            warning = self._asr_error or "ASR 尚未就绪"
            with self._lock:
                if number >= self._transcript_number:
                    self._transcript_number = number
                    self._transcript = f"ASR 不可用，第 {number} 段未转写"
                self._asr_stats["skipped"] += 1
                self._task = "等待 ASR 就绪"
                for item in self._segment_history:
                    if item.get("number") == number:
                        item["transcript"] = f"ASR 不可用：{warning}"
                        break
            self._set_summary(
                number, f"判定：不确定 · 环境声：关闭 · 置信度 0.0\n依据：{warning}"
            )
            self._publish_decision(
                number, "unclear", "close", 0.0,
                f"ASR 未就绪（{warning}），无法获得语义判定", "local",
                epoch_id=epoch_id,
            )
            self._finish_training(number, {
                "asr": {"skipped": f"ASR 未就绪：{warning}"},
                "labels": {"teacher": {"dialogue": "unclear", "ambient": "close",
                                        "confidence": 0.0,
                                        "reason": f"ASR 未就绪：{warning}",
                                        "source": "local"}},
            })
            return

        # trim_start 是相对本段音频的偏移；声纹窗口用的是绝对流样本号。
        # 连续中继里 start_sample 几乎总 >0，漏加它会让所有轮次都归 unknown。
        segments = self._speaker_segments(trimmed, rate, start_sample + trim_start)
        labels = sorted({speaker for speaker, _ in segments})
        # 声纹全部 unknown 时 LLM 看不到轮次结构；用停顿切成多轮再送 ASR/LLM。
        if not labels or labels == ["unknown"]:
            pause_turns = self._split_unknown_turns(
                trimmed, rate, regions, trim_start
            )
            if len(pause_turns) >= 2:
                segments = pause_turns
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
                "remaining": len(segments), "results": {}, "raw_results": {},
                "asr_notes": [], "epoch_id": epoch_id, "llm_context": llm_context,
            }
            self._task = f"Qwen ASR 正在逐轮转写第 {number} 段候选人声"
            self._transcript = "正在转写并对齐 self / other / unknown…"
        try:
            for index, (speaker, segment_audio) in enumerate(segments):
                request_id = f"{event_id}-{index}"
                segment_audio, asr_gain_db = self._prepare_asr_audio(segment_audio)
                turn_seconds = len(segment_audio) / 2 / max(rate, 1)
                with self._lock:
                    self._pending[request_id] = (
                        event_id, index, speaker, turn_seconds, time.monotonic(),
                    )
                    for item in self._segment_history:
                        if item.get("number") == number:
                            item["asr_gain_db"] = max(
                                float(item.get("asr_gain_db", 0.0)), asr_gain_db
                            )
                            break
                max_tokens = self._asr_token_budget(turn_seconds)
                payload = {
                    "id": request_id, "rate": rate,
                    "pcm_base64": base64.b64encode(segment_audio).decode("ascii"),
                    "max_new_tokens": max_tokens,
                }
                with self._asr_write_lock:
                    process.stdin.write(json.dumps(payload) + "\n")
                    process.stdin.flush()
            return
        except OSError as exc:
            with self._lock:
                self._asr_error = f"写入 ASR worker 失败：{exc}"
                if number >= self._transcript_number:
                    self._transcript_number = number
                    self._transcript = f"ASR worker 写入失败：{exc}"
                self._pending_events.pop(event_id, None)
            self._publish_decision(
                number, "unclear", "close", 0.0,
                f"ASR worker 写入失败：{exc}", "local",
                epoch_id=epoch_id,
            )
            self._finish_training(number, {
                "asr": {"error": f"ASR worker 写入失败：{exc}"},
                "labels": {"teacher": {"dialogue": "unclear", "ambient": "close",
                                        "confidence": 0.0,
                                        "reason": f"ASR worker 写入失败：{exc}",
                                        "source": "local"}},
            })

    @classmethod
    def _asr_token_budget(cls, speech_seconds: float) -> int:
        """按语音时长限制生成 tokens。模型默认 448 对一段短对话太长，容易进入重复循环。"""
        return int(min(cls._MAX_ASR_TOKENS, max(24, speech_seconds * 12)))

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
        """按声纹窗口切分候选音频，并把同一句里的 unknown 碎片并回两侧身份。

        2026-10-22 修正（用户现场："我对着说话，你告诉我 unknown？"）：
        旧实现按 0.75 s 切片逐片贴标签，标签一变就切段送 ASR。用户一句 1–2 s 的自说，
        前半片落在 self 窗、后半片落在混音/静音窗，同一句就被切成 `self` + `unknown`
        两段，面板上看起来"没分清"。

        现在保留逐片判定（声纹窗中点落在片内才投票，冲突/缺失为 unknown），再做一次
        **边界平滑**：短 unknown 片如果只挨着一侧明确身份（self 或 other），就并入那一侧；
        self 与 other 相邻的 unknown 片保持 unknown（那是未解决的边界，不能猜）。
        只改归属、不改 WavLM 判定；混音轮次仍可能判错，但同一句不再被切碎。
        """
        snapshot = {}
        if self._voiceprint_snapshot is not None:
            try:
                snapshot = self._voiceprint_snapshot() or {}
            except Exception:  # noqa: BLE001 - 身份不可用时必须退回 unknown
                snapshot = {}
        windows = snapshot.get("history") if snapshot.get("ready") else []
        windows = windows if isinstance(windows, list) else []
        total_samples = len(audio) // 2
        if total_samples <= 0:
            return [("unknown", audio)]
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
            # 两个重叠窗给出不同身份 = 边界未解决；没有窗 = 没有证据。
            label = next(iter(labels)) if len(labels) == 1 else "unknown"
            part = audio[offset * 2:end * 2]
            if slices and slices[-1][0] == label:
                slices[-1] = (label, slices[-1][1] + part)
            else:
                slices.append((label, part))

        # 边界平滑：一片以内（约 1.5 s）的 unknown，如果左右只有一侧是明确身份，
        # 就并入那一侧；左右身份不同则保留 unknown。最多迭代到稳定。
        bridge_limit = int(rate * 0.8) * 2  # 一片 0.75 s，留一点容差
        for _ in range(3):
            bridged: list[tuple[str, bytes]] = []
            for index, (label, part) in enumerate(slices):
                if label == "unknown" and len(part) <= bridge_limit:
                    left = slices[index - 1][0] if index > 0 else None
                    right = slices[index + 1][0] if index + 1 < len(slices) else None
                    left_id = left in ("self", "other")
                    right_id = right in ("self", "other")
                    if left_id and right_id:
                        if left == right:
                            label = left
                    elif left_id:
                        label = left
                    elif right_id:
                        label = right
                if bridged and bridged[-1][0] == label:
                    bridged[-1] = (label, bridged[-1][1] + part)
                else:
                    bridged.append((label, part))
            if bridged == slices:
                break
            slices = bridged

        # Very short turns are unreliable for both WavLM and ASR; retain their audio but
        # remove the identity claim by folding them into unknown. Exception: self ——
        # 佩戴者的短应答（"嗯""好"）恰恰是 voice 门/LLM 要的证据，折叠成 unknown 等于
        # 主动销毁它（2026-10-22 事故复盘：全段 speaker 只有 other/unknown，门永不 latch）。
        minimum = int(rate * 0.45) * 2
        normalized = [
            (speaker if (len(pcm) >= minimum or speaker == "self") else "unknown", pcm)
            for speaker, pcm in slices if pcm
        ]
        merged: list[tuple[str, bytes]] = []
        for speaker, pcm in normalized:
            if merged and merged[-1][0] == speaker:
                merged[-1] = (speaker, merged[-1][1] + pcm)
            else:
                merged.append((speaker, pcm))
        return merged or [("unknown", audio)]

    # ------------------------------------------------------------------ 语音证据

    @staticmethod
    def _telemetry_speech_regions(telemetry: list[dict[str, object]], rate: int,
                                  start_sample: int, total_samples: int
                                  ) -> list[tuple[int, int]]:
        """设备遥测的语音证据：连续 >=4 帧 voice_likelihood 高才算一段。

        单帧尖峰（噪声/撞击）不再能触发 ASR；键盘/风/撞击事件直接否决。
        """
        if not telemetry:
            return []
        centers: list[int] = []
        run: list[int] = []
        runs: list[list[int]] = []
        for item in telemetry:
            likelihood = float(item.get("voice_likelihood") or 0.0)
            event = str(item.get("event") or "")
            sample = int(item.get("sample") or 0) - start_sample
            voiced = likelihood >= 0.65 and event not in ("keyboard", "wind", "artifact")
            if voiced:
                run.append(sample)
            else:
                if run:
                    runs.append(run)
                    run = []
        if run:
            runs.append(run)
        intervals: list[tuple[int, int]] = []
        for run in runs:
            # 连续 4 帧 = 400 ms；真实说话会持续更久，偶发噪声尖峰不会。
            if len(run) < 4:
                continue
            centers.extend(run)
        if not centers:
            return []
        # 帧表示"最近约 100 ms"的证据；前后各留 150 ms 容差。
        half_window = int(rate * 0.15)
        for center in centers:
            begin = max(0, min(total_samples, center - half_window))
            end = max(0, min(total_samples, center + half_window))
            if end > begin:
                intervals.append((begin, end))
        return _merge_intervals(intervals, int(rate * 0.50))

    @staticmethod
    def _audio_speech_regions(pcm: bytes, rate: int) -> list[tuple[int, int]]:
        """纯 Python 能量 VAD（上位机不依赖 numpy）：20 ms 帧、20 ms 步进。

        它只做"这段音频里有没有明显高于本底的能量"，用来兜住设备遥测漏报的近场人声；
        语言判别仍交给 ASR。稳态噪声（风扇/空调）因为最高帧与 20 分位本底差太小会被判无语音。
        """
        total = len(pcm) // 2
        if total < int(rate * 0.3) or rate <= 0:
            return []
        samples = array("h")
        samples.frombytes(pcm[:total * 2])
        if sys.byteorder != "little":
            samples.byteswap()
        frame = max(1, int(rate * 0.02))
        hop = max(1, int(rate * 0.02))
        levels: list[tuple[int, float]] = []
        for begin in range(0, total - frame + 1, hop):
            window = samples[begin:begin + frame]
            amplitude = max(max(window), -min(window))
            levels.append((begin, 20.0 * math.log10(max(amplitude, 1.0) / 32768.0)))
        if len(levels) < 5:
            return []
        ordered = sorted(level for _, level in levels)
        floor = ordered[int(len(ordered) * 0.2)]
        peak = ordered[-1]
        # 稳态噪声：最响帧也只比本底高不到 8 dB。人声的峰值/本底差通常 > 12 dB。
        if peak - floor < 8.0:
            return []
        threshold = max(floor + 6.0, -58.0)
        voiced = [(begin, begin + frame) for begin, level in levels if level >= threshold]
        if not voiced:
            return []
        merged = _merge_intervals(voiced, int(rate * 0.50))
        minimum = int(rate * 0.5)
        return [(begin, end) for begin, end in merged if end - begin >= minimum]

    @staticmethod
    def _split_unknown_turns(audio: bytes, rate: int,
                             regions: list[tuple[int, int]],
                             trim_start: int) -> list[tuple[str, bytes]]:
        """声纹不可用时，用静音停顿把片段切成"轮次"，供 LLM 做语义判断。

        声纹给不出 self/other 时，逐字稿只有一大段文字，LLM 无法判断是不是对话。
        这里按能量 VAD 的语音区间切分：间隔 >=0.35 s 视为换轮；过短的碎块并回上一轮。
        身份仍然标 unknown——这不是说话人分离，只是给语义判定提供轮次结构。
        """
        total = len(audio) // 2
        windows: list[list[int]] = []
        for begin, end in regions:
            begin = max(0, int(begin) - trim_start)
            end = min(total, int(end) - trim_start)
            if end - begin < int(rate * 0.3):
                continue
            if windows and begin - windows[-1][1] <= int(rate * 0.35):
                windows[-1][1] = max(windows[-1][1], end)
            else:
                windows.append([begin, end])
        if len(windows) < 2:
            return [("unknown", audio)] if audio else []
        chunks: list[tuple[str, bytes]] = []
        minimum = int(rate * 0.45) * 2
        for begin, end in windows:
            part = audio[begin * 2:end * 2]
            if not part:
                continue
            if chunks and len(part) < minimum:
                chunks[-1] = ("unknown", chunks[-1][1] + part)
            else:
                chunks.append(("unknown", part))
        return chunks if len(chunks) >= 2 else [("unknown", audio)]

    @classmethod
    def _event_blocked_regions(cls, telemetry: list[dict[str, object]], rate: int,
                               start_sample: int, total_samples: int
                               ) -> list[tuple[int, int]]:
        """设备明确判为键盘/风/异响的时段。

        2026-10-26 用户口径："S3 判断是键盘的话，就不用转写了"——设备事件必须和
        ASR 门控耦合。实测这批窗口送云端只会换来 HTTP 400 空体（服务端判无人声），
        面板上就表现成"一直转写失败"。
        """
        if not telemetry or total_samples <= 0 or rate <= 0:
            return []
        half = int(rate * 0.15)  # 遥测帧代表"最近约 100 ms"
        intervals: list[tuple[int, int]] = []
        for item in telemetry:
            if str(item.get("event") or "") not in cls._BLOCKING_EVENTS:
                continue
            center = int(item.get("sample") or 0) - start_sample
            begin = max(0, min(total_samples, center - half))
            end = max(0, min(total_samples, center + half))
            if end > begin:
                intervals.append((begin, end))
        return _merge_intervals(intervals, int(rate * 0.10))

    @staticmethod
    def _subtract_intervals(regions: list[tuple[int, int]],
                            blocked: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """从语音区间里挖掉设备判定的非人声时段（保留其余人声）。"""
        if not blocked:
            return list(regions)
        out: list[tuple[int, int]] = []
        for begin, end in regions:
            pieces = [(begin, end)]
            for b0, b1 in blocked:
                remaining: list[tuple[int, int]] = []
                for a0, a1 in pieces:
                    if b1 <= a0 or b0 >= a1:
                        remaining.append((a0, a1))
                        continue
                    if a0 < b0:
                        remaining.append((a0, b0))
                    if b1 < a1:
                        remaining.append((b1, a1))
                pieces = remaining
            out.extend(pieces)
        return [(b, e) for b, e in out if e > b]

    @classmethod
    def _select_speech_regions(cls, pcm: bytes, rate: int,
                               telemetry: list[dict[str, object]],
                               start_sample: int) -> tuple[list[tuple[int, int]], float, str]:
        """选择用于转写的语音区间。返回 (区间, 有效秒数, 证据说明)。

        2026-10-26：设备判为键盘/风/异响的时段先从人声证据里挖掉，再判断有没有
        足够语音——否则这些窗口会原样送去云端，换回 400。
        """
        total = len(pcm) // 2
        blocked = cls._event_blocked_regions(telemetry, rate, start_sample, total)
        device = cls._subtract_intervals(
            cls._telemetry_speech_regions(telemetry, rate, start_sample, total), blocked
        )
        audio = cls._subtract_intervals(cls._audio_speech_regions(pcm, rate), blocked)
        device_s = _intervals_seconds(device, rate)
        audio_s = _intervals_seconds(audio, rate)
        blocked_s = _intervals_seconds(blocked, rate)
        suffix = f"；已排除键盘/风噪 {blocked_s:.1f}s" if blocked_s > 0 else ""
        if device_s >= cls._MIN_SPEECH_S:
            return device, device_s, (
                f"设备语音证据 {device_s:.1f}s（能量证据 {audio_s:.1f}s）{suffix}"
            )
        if audio_s >= cls._MIN_SPEECH_S:
            return audio, audio_s, f"能量语音证据 {audio_s:.1f}s（设备遥测不足）{suffix}"
        return [], max(device_s, audio_s), (
            f"设备语音证据 {device_s:.1f}s / 能量证据 {audio_s:.1f}s{suffix}"
        )

    @classmethod
    def _trim_to_speech(cls, pcm: bytes, rate: int,
                        regions: list[tuple[int, int]]) -> tuple[bytes, int]:
        """只裁掉首尾无人声部分，保留中间原始时间轴（声纹窗口按原始样本号对齐）。"""
        if not regions:
            return b"", 0
        total = len(pcm) // 2
        pad = int(rate * 0.2)
        begin = max(0, regions[0][0] - pad)
        end = min(total, regions[-1][1] + pad)
        max_samples = int(cls._MAX_ASR_SECONDS * rate)
        if end - begin > max_samples:
            end = begin + max_samples
        if end - begin < int(rate * 0.3):
            return b"", begin
        return pcm[begin * 2:end * 2], begin

    # ------------------------------------------------------------------ 去幻觉

    @classmethod
    def _filter_asr_text(cls, text: str, duration_s: float = 0.0,
                         speaker: str | None = None) -> tuple[str, str]:
        """过滤 ASR 幻觉：重复循环、纯语气词、单字噪声。返回 (可显示文本, 说明)。

        speaker == "self" 时放宽：佩戴者的短应答（"嗯""啊"）是 voice 门和 LLM
        判定"你接话了"的关键证据，只折叠、不整句丢弃（2026-10-22 事故复盘：
        self 的"嗯"被语气词过滤抹掉，LLM 因此全程判"无 self 回应"，环境声门
        从未 latch）。other/unknown 仍按旧规则过滤，防噪声幻觉污染。
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return "", "空输出"
        # 模型偶尔带 markdown 代码块/引号。
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip().strip('"“”')
        # 中文之间不该有空格（Qwen 的 token 空格不应显示成断字）。
        cleaned = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", cleaned)
        base = _cjk_len(cleaned)
        if base == 0:
            return "", "只有标点/符号"
        # 2~40 字的重复单元至少出现 3 次，视为重复循环。
        loop = re.compile(r"(.{2,40}?)(?:[\s，。、！？,.!?；;：:…—\-]*\1){2,}")
        collapsed, replaced = loop.subn(lambda match: match.group(1), cleaned)
        collapsed = re.sub(r"(.)\1{3,}", r"\1\1", collapsed)
        removed = base - _cjk_len(collapsed)
        # 长时间片段里的重复循环基本是模型幻觉（噪声/静音）。但很短的强调式重复
        # （"好的好的好的"）是正常口语：只折叠、不整段丢弃。
        if removed / max(base, 1) > 0.5 and duration_s >= 3.0:
            return "", "重复幻觉已丢弃"
        effective = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", collapsed)
        if not effective:
            return "", "只有标点/符号"
        if effective in _SHORT_ANSWER_WORDS:
            return collapsed.strip(), "短应答保留"
        if all(char in _FILLER_CHARS for char in effective) or effective in _FILLER_WORDS:
            if speaker == "self":
                return collapsed.strip(), "语气词·self保留"
            return "", "只有语气词"
        if len(effective) <= 1 and duration_s >= 1.0:
            if speaker == "self":
                return collapsed.strip(), "单字·self保留"
            return "", "有效字数不足"
        truncated = False
        if len(effective) > cls._MAX_TRANSCRIPT_CHARS:
            collapsed = collapsed[:cls._MAX_TRANSCRIPT_CHARS - 1] + "…"
            truncated = True
        notes = []
        if replaced:
            notes.append("已折叠重复片段")
        if removed / max(base, 1) > 0.2:
            notes.append(f"重复占 {removed / max(base, 1):.0%}")
        if truncated:
            notes.append("超长已截断")
        return collapsed.strip(), "；".join(notes)

    # ------------------------------------------------------------------ 判定

    def _publish_decision(self, number: int, dialogue: str, ambient: str,
                          confidence: float, reason: str, source: str,
                          epoch_id: int | None = None) -> None:
        """发布环境声授权判定。

        * 旧片段的判定不能覆盖新片段（LLM 并发请求可能乱序返回）；
        * 判定绑定派发时的对话轮次 `epoch_id`：上一轮对话/上一次录音的 LLM 后来才回来，
          也不能给当前轮次授权。
        """
        with self._lock:
            if epoch_id is not None and int(epoch_id) != self._authorization_epoch_id:
                return
            if number < self._decision_number:
                return
            self._decision_number = max(self._decision_number, number)
            self._decision = {
                "number": number,
                "dialogue": dialogue if dialogue in ("yes", "no", "unclear") else "unclear",
                "ambient": "open" if ambient == "open" and dialogue == "yes" else "close",
                "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
                "reason": str(reason or "")[:160],
                "source": source if source in ("llm", "local") else "local",
                "epoch_id": self._authorization_epoch_id if epoch_id is None else int(epoch_id),
                "at": time.monotonic(),
            }
            self._llm_context_verdicts.append({
                "segment": number,
                "dialogue": str(self._decision["dialogue"]),
                "ambient": str(self._decision["ambient"]),
                "source": str(self._decision["source"]),
                "reason": str(self._decision["reason"])[:80],
            })
            # 对话状态机（只认 LLM 判定；本地"无语音证据"不算语义结论）。
            if source == "llm":
                if dialogue == "yes":
                    self._dialogue_confirmed = True
                    self._consecutive_no = 0
                elif dialogue == "no":
                    self._consecutive_no += 1
            self._task = "等待下一段 S3 候选人声"
            decision = dict(self._decision)
        # 记忆写入放在锁外：memo 有自己的锁，也不能因为它失败而影响判定/授权。
        callback = self._on_decision
        if callback is not None:
            try:
                callback(decision)
            except Exception:  # noqa: BLE001 - 记忆是旁路，失败不改变判定
                pass

    @staticmethod
    def _verdict_text(dialogue: str) -> str:
        return {"yes": "对话", "no": "非对话", "unclear": "不确定"}.get(dialogue, "不确定")

    @classmethod
    def _format_decision(cls, decision: dict[str, object]) -> str:
        dialogue = str(decision.get("dialogue") or "unclear")
        ambient = "开放" if decision.get("ambient") == "open" else "关闭"
        source = "LLM" if decision.get("source") == "llm" else "本地回退"
        confidence = float(decision.get("confidence") or 0.0)
        return (
            f"判定：{cls._verdict_text(dialogue)} · 环境声：{ambient} · "
            f"置信度 {confidence:.2f} · {source}\n"
            f"依据：{decision.get('reason') or '—'}"
        )

    def ambient_authorization(self, now: float | None = None) -> dict[str, object]:
        """环境声执行授权（D34：快路径本地决定，LLM 只有迟到否决权）。

        * 没有判定 / 判定属于上一轮 → **放行**，走端侧快路径（毫秒级）；
        * LLM 新鲜 `dialogue=no` → 否决，关闭环境声（迟到撤销，是 demo 要展示的
          "慢判定纠正快动作"）；
        * LLM `yes` → 确认；`unclear` / 本地判定 → 不否决，保持快路径；
        * LLM 不可用/ASR 死亡 → 退化为纯本地，不阻塞。

        这与上一版"LLM 授权才开"相反：任何网络/ASR 延迟都不能进入首次响应路径。
        """
        with self._lock:
            decision = dict(self._decision) if self._decision is not None else None
            current_epoch = self._authorization_epoch_id
        base: dict[str, object] = {
            "ttl_s": self._DECISION_TTL_S,
            "age_s": None,
            "decision": decision,
            "veto": False,
        }
        if not self.configured:
            return {
                **base, "allow": True, "state": "local_only",
                "reason": "LLM 未配置：环境声由端侧快路径/声纹门控决定",
            }
        if decision is None:
            return {
                **base, "allow": True, "state": "fast_local",
                "reason": "本地快路径：LLM 语义判定尚未返回（不阻塞首次响应）",
            }
        age = max(0.0, (time.monotonic() if now is None else now) - float(decision["at"]))
        base["age_s"] = round(age, 1)
        base.update({
            "verdict_text": self._verdict_text(str(decision.get("dialogue"))),
            "confidence": decision.get("confidence"),
            "source": decision.get("source"),
            "number": decision.get("number"),
        })
        if int(decision.get("epoch_id", -1)) != current_epoch:
            return {
                **base, "allow": True, "state": "fast_local",
                "reason": "本地快路径：最近判定属于上一轮对话，已忽略",
            }
        if decision.get("source") != "llm":
            # 本地"无有效语音/证据不足"是确定性的负证据：它不能像 LLM 一样否决
            # "语义上是不是对话"，但可以否决"这段音频值得开环境声"。否则快路径会在
            # 纯噪声里一直开着，而 ASR 过滤空时又不会调用 LLM 来纠正。
            if decision.get("dialogue") == "no":
                return {
                    **base, "allow": False, "state": "local_veto", "veto": True,
                    "reason": f"本地证据否决：{decision.get('reason') or '—'}",
                }
            return {
                **base, "allow": True, "state": "fast_local",
                "reason": "本地快路径：本地回退判定不参与语义否决",
            }
        if decision.get("dialogue") == "no":
            # 对话状态机（2026-10-26 用户口径）：已确认"对话进行中"时，单次 no 常把
            # "本段单向/无问答"误当结束，不能立即否决；连续两次 no 才确认结束。
            if self._dialogue_confirmed and self._consecutive_no < 2:
                return {
                    **base, "allow": True, "state": "llm_confirmed",
                    "reason": f"对话进行中，单次 no 暂不否决：{decision.get('reason') or '—'}",
                }
            self._dialogue_confirmed = False
            self._consecutive_no = 0
            return {
                **base, "allow": False, "state": "llm_veto", "veto": True,
                "reason": f"LLM 迟到否决：{decision.get('reason') or '—'}",
            }
        if age > self._DECISION_TTL_S:
            # 判定过期 = 对话大概率已中断：撤销"进行中"状态，后续单次 no 才能即时否决。
            self._dialogue_confirmed = False
            self._consecutive_no = 0
            return {
                **base, "allow": True, "state": "fast_local",
                "reason": f"LLM 判定已过期 {age:.0f}s，忽略；端侧快路径继续",
            }
        if decision.get("dialogue") == "yes":
            return {
                **base, "allow": True, "state": "llm_confirmed",
                "reason": f"LLM 确认对话：{decision.get('reason') or '—'}",
            }
        # unclear：不打断进行中的对话（保持 llm_confirmed，让门继续开），
        # 未确认时仍按不确定处理。
        if self._dialogue_confirmed:
            return {
                **base, "allow": True, "state": "llm_confirmed",
                "reason": f"对话进行中，本段不确定不打断：{decision.get('reason') or '—'}",
            }
        return {
            **base, "allow": True, "state": "llm_unclear",
            "reason": f"LLM 不确定，保持本地快路径：{decision.get('reason') or '—'}",
        }

    # ------------------------------------------------------------------ LLM

    @staticmethod
    def _parse_decision(text: str) -> dict[str, object] | None:
        """从模型输出里提取一行 JSON 判定；不合法就返回 None（调用方走本地回退）。"""
        if not text:
            return None
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", candidate).strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(candidate[start:end + 1])
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        raw_dialogue = str(data.get("dialogue") or data.get("判定") or "").strip().lower()
        dialogue_map = {
            "yes": "yes", "no": "no", "unclear": "unclear",
            "对话": "yes", "非对话": "no", "不确定": "unclear",
            "true": "yes", "false": "no",
        }
        dialogue = dialogue_map.get(raw_dialogue, "")
        if not dialogue:
            return None
        raw_ambient = str(data.get("ambient") or data.get("环境声") or "").strip().lower()
        ambient = "open" if raw_ambient in ("open", "yes", "true", "开放") else "close"
        if dialogue != "yes":
            ambient = "close"
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason") or data.get("依据") or "").strip()[:120]
        return {
            "dialogue": dialogue,
            "ambient": ambient,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": reason,
        }

    @staticmethod
    def _interaction_features(speaker_turns, llm_context) -> dict:
        """把声纹轮次压成"对话交互"特征，辅助 LLM 判断。

        LLM 原来只能从零碎文本猜是不是一问一答；这里显式给出本段与跨段的
        self/other 交替结构。unknown 不计入交替（身份不明不能当轮次证据）。
        """
        turns = speaker_turns or []
        counts = {"self": 0, "other": 0, "unknown": 0}
        order: list[str] = []
        for speaker, _text in turns:
            if speaker in counts:
                counts[speaker] += 1
            if speaker in ("self", "other"):
                order.append(speaker)
        alternating = any(a != b for a, b in zip(order, order[1:]))
        recent: list[str] = []
        if isinstance(llm_context, dict):
            for item in (llm_context.get("recent_turns") or [])[-8:]:
                if isinstance(item, dict) and item.get("speaker") in ("self", "other"):
                    recent.append(str(item["speaker"]))
        recent_alternating = any(a != b for a, b in zip(recent, recent[1:]))
        return {
            "self_turns": counts["self"],
            "other_turns": counts["other"],
            "unknown_turns": counts["unknown"],
            "alternating": alternating,
            "recent_alternating": recent_alternating,
        }

    def _summarize_event(self, number: int, seconds: float, chunks: int,
                         chunks_lost: int, telemetry: list[dict[str, object]],
                         transcript: str = "",
                         speaker_turns: list[tuple[str, str]] | None = None,
                         epoch_id: int = 0, had_raw: bool = False,
                         raw_text: str = "",
                         llm_context: dict[str, object] | None = None) -> None:
        if not self.configured:
            return
        started = time.monotonic()
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
            "event": "s3_candidate_segment_finished",
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
            #: 用户记忆摘要（≤300 字）。空串 = 没有可用记忆；LLM 只把它当弱先验。
            "memo": self._memo_context() if self._memo_context is not None else "",
            "transcript": transcript,
            "speaker_turns": speaker_turns or [],
            "identity_available": any(
                speaker in ("self", "other") for speaker, _ in (speaker_turns or [])
            ),
            # 声纹轮次结构（2026-10-23 用户要求"对话交互辅助 LLM 判断"）：
            # 只统计身份明确的 self/other；unknown 不参与交替判断。
            "interaction": self._interaction_features(speaker_turns, llm_context),
            # 跨片段上下文：同一轮对话此前最近的转写轮次与判定。6 s 分段常把
            # "对方一句/我一句"切到两个请求里，没有它每段都像单方独白。
            "context": llm_context or {"turn_count": 0, "recent_turns": [],
                                        "previous_verdicts": []},
        }
        fallback = self._local_summary(facts)
        prompt = (
            "你是耳机对话判定器，负责维护一段对话的进行状态（不是孤立地给每 6 秒打分）。"
            "self=已注册佩戴者；other=只能证明非佩戴者，不能证明是同一个人；unknown=身份不确定。"
            "dialogue 三个值：yes=对话进行中；no=对话已结束或不是对话；unclear=开场/身份不足/无法判断。"
            "只输出一个单行 JSON 对象，不要 markdown、代码块、换行、前后缀或解释；"
            "必须且只能含四个键：dialogue（yes|no|unclear）、ambient（open|close）、"
            "confidence（0.0~1.0 的数字）、reason（不超过40个字符的字符串）。"
            "规则：只有 dialogue=yes 才 ambient=open；no/unclear 一律 ambient=close。"
            "判 yes（对话进行中）的情形："
            "1) self 与 other 出现有语义关联的轮流回应（facts.interaction 的"
            "   alternating / recent_alternating 为 true，身份少量 unknown 也算）；"
            "2) 前文 context.previous_verdicts 已判 yes，且本段是同一场对话的延续——"
            "   哪怕是某一方连续陈述、另一方只是偶尔短回应或在听（面试/讲解/汇报都算），"
            "   也判 yes 保持打开；"
            "3) identity_available=false 但逐字稿里出现至少两个由静音分开、语义像问答/接话的轮次。"
            "判 no（已结束/不是对话）的情形：明确对话结束（长静音后话题完全改变/换了一群人）、"
            "或从头就是媒体/播客/电视播音、单人对着空气的连续独白且前文从未确认过对话。"
            "判 unclear：开场、身份不足、或无法确定是否同一场对话的延续。"
            "重要：前文已判 yes 时，本段单独看不清不要轻易改判 no——只有明确证据表明对话结束才 no；"
            "本段是单向陈述本身不构成结束，除非前文从未确认过对话。"
            "facts.interaction 是声纹轮次结构；s3_ambient_open_intent_frames 只是 S3 意图，"
            "不等于对话证据，也不等于实际执行。"
            "facts.memo 是历史记忆（可能有偏/过期），只作弱先验；与本段逐字稿/遥测冲突时"
            "以本段为准，不能凭记忆判对话或开放环境声。数据："
            + json.dumps(facts, ensure_ascii=False)
        )
        try:
            result = self._request_llm(prompt)
            raw = "".join(
                item.get("text", "") for item in result.get("content", [])
                if item.get("type") == "text"
            ).strip()
            if not raw:
                stop_reason = result.get("stop_reason") or "未知结束原因"
                content_types = ",".join(
                    str(item.get("type", "unknown")) for item in result.get("content", [])
                ) or "空内容"
                raise RuntimeError(f"模型未返回正文（{stop_reason}；{content_types}）")
            parsed = self._parse_decision(raw)
            if parsed is None:
                raise RuntimeError(f"模型未按 JSON 协议判定：{raw[:80]}")
            decision = {**parsed, "source": "llm"}
            self._set_summary(number, self._format_decision(decision), epoch_id=epoch_id)
            with self._lock:
                self._llm_warning = ""
            self._publish_decision(
                number, str(parsed["dialogue"]), str(parsed["ambient"]),
                float(parsed["confidence"]), str(parsed["reason"]), "llm",
                epoch_id=epoch_id,
            )
            self._finish_training(number, {
                "llm": {
                    "model": self.model,
                    "context": llm_context or {},
                    "facts": {key: facts[key] for key in (
                        "session", "audio_seconds", "conversation_states",
                        "environments", "s3_ambient_open_intent_frames",
                        "mean_voice_likelihood", "transcript", "speaker_turns",
                        "identity_available",
                    )},
                    "raw_response": raw,
                    "decision": {**parsed, "source": "llm"},
                    "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                },
                "labels": {"teacher": {**parsed, "source": "llm"}},
            })
        except (OSError, ValueError, RuntimeError, KeyError, AttributeError, TypeError,
                urllib.error.HTTPError) as exc:
            # ASR 与事件账本已经成功，LLM 拒答/超时不应把整段显示为没转写；
            # 但环境声授权必须 fail-closed：LLM 没给明确判定就不开放。
            self._set_summary(
                number,
                fallback + f"\nLLM 未给出可解析判定，环境声退回本地快路径：{exc}",
                epoch_id=epoch_id,
            )
            with self._lock:
                self._llm_warning = f"LLM 判定失败，已回退本地摘要：{exc}"
                self._task = "等待下一段 S3 候选人声"
            self._publish_decision(
                number, "unclear", "close", 0.0,
                f"LLM 未给出可解析判定：{str(exc)[:80]}", "local",
                epoch_id=epoch_id,
            )
            self._finish_training(number, {
                "llm": {
                    "model": self.model,
                    "context": llm_context or {},
                    "error": str(exc)[:200],
                    "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                },
                "labels": {"teacher": {"dialogue": "unclear", "ambient": "close",
                                        "confidence": 0.0,
                                        "reason": f"LLM 失败：{str(exc)[:80]}",
                                        "source": "local"}},
            })

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

    # ------------------------------------------------------------------ 快照

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
            decision = dict(self._decision) if self._decision is not None else None
            # 同一把 RLock 内计算授权，避免界面把 decision #N 和 #N+1 的授权拼在一起。
            authorization = self.ambient_authorization()
            context = self._context_facts()
        texts = {
            "not_configured": "未配置",
            "checking": f"正在连接 · {self.model}",
            "ready": f"已连接 · {self.model}",
            "error": "连接失败",
        }
        decision_snapshot = None
        if decision is not None:
            age = max(0.0, time.monotonic() - float(decision["at"]))
            decision_snapshot = {
                "number": decision.get("number"),
                "dialogue": decision.get("dialogue"),
                "verdict_text": self._verdict_text(str(decision.get("dialogue"))),
                "ambient": decision.get("ambient"),
                "ambient_text": "开放" if decision.get("ambient") == "open" else "关闭",
                "confidence": decision.get("confidence"),
                "reason": decision.get("reason"),
                "source": decision.get("source"),
                "age_s": round(age, 1),
            }
        return {
            "available": state == "ready",
            "state": state,
            "state_text": texts[state],
            "task": self._task,
            "transcript": self._transcript,
            "summary_state": self._summary,
            "today_sessions": self._today_sessions,
            "provider": self.provider,
            "asr_provider": self.asr_provider,
            "model": self.model,
            "error": error,
            "asr_ready": self._asr_ready,
            "asr_model": self._asr_model,
            "asr_error": self._asr_error,
            "asr_stats": {
                **dict(self._asr_stats),
                "latency_p50_ms": round(
                    sorted(self._asr_latency_ms)[len(self._asr_latency_ms) // 2], 0
                ) if self._asr_latency_ms else None,
                "latency_n": len(self._asr_latency_ms),
            },
            "llm_warning": self._llm_warning,
            "decision": decision_snapshot,
            "ambient_authorization": authorization,
            "llm_context": {
                "turn_count": context["turn_count"],
                "verdict_count": len(context["previous_verdicts"]),
                "recent_turns": context["recent_turns"][-3:],
                "recent_verdicts": context["previous_verdicts"][-2:],
            },
            "training": None if self._training is None else {
                "enabled": True,
                "session": str(getattr(self._training, "session_dir", "")),
                "segments": getattr(self._training, "segments_written", 0),
            },
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
