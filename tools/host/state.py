"""上位机的设备状态归约层。

这一层回答一个具体问题：**把一串帧变成"设备现在处于什么状态"，并且把这个判断的依据
一起留下来。**

三条刻意的设计，都是被这个项目的既有教训逼出来的：

1. **丢帧必须显式报出，不能静默。** `tools/record_from_device.py` 已经确立了这条纪律
   （丢块会让声纹抖动与信噪比失真，所以丢块录音不能用于结论）。控制帧的序号同样连续
   （实测 6 秒 41 帧 gap=0），所以这里也对序号做同样的检查：一旦 USB 丢帧，面板上必须
   看得到，而不是让曲线悄悄断开。

2. **陈旧和断开是两件事。** 端口打开但设备不吐数据，是这个项目踩过的坑（"节点在≠设备在"），
   而现在还多了一个更隐蔽的版本：DTR 未置位时端口能开、能写、**永远读不到一个字节**
   （见 tools/monitor.sh 的注释）。所以判定用"多久没收到帧"，而不是"端口是否打开"。

3. **中文只在这一层映射。** 固件的 `state` / `environment` 是英文枚举（OBSERVING /
   NOISY …），设备屏幕已经把它们翻成中文（main/demo_ui.cpp）。上位机要跟屏幕说同一句话，
   就必须用同一套词：`正在感知` / `噪声` …。翻译放在这里而不是前端，是为了让"屏幕说的"
   与"面板说的"可以被同一条断言测出来。
"""

from __future__ import annotations

import math
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .protocol import (
    CAPTURE_RATE_HZ,
    CONTEXT_CLASSES,
    DEMO_SPEECH_GAIN_DB,
    KNOWN_ENVIRONMENTS,
    KNOWN_STATES,
    MAX_NOISE_REDUCTION_DB,
    MAX_SPEECH_GAIN_DB,
    CaptureEvent,
    CommandFrame,
    LatencyReport,
    MicChunk,
    ParseError,
    RouteEvent,
    parse_line,
)

#: 控制节拍。与固件一致：正常 100 ms，录音时放宽到 250 ms（main/app_main.cpp）。
CONTROL_PERIOD_MS = 100.0
CAPTURE_PERIOD_MS = 250.0
#: 与串口层的 no-data 判据一致。短暂的路由探测、浏览器切页或一次调度抖动不应
#: 把仍在工作的设备判死；持续 10 秒没有控制帧才显示断流告警。
STALE_TIMEOUT_MS = 10_000.0
#: "这一段静默不正常"的门槛。控制节拍 100 ms，1 秒 = 丢 10 帧——正常抖动到不了这里，
#: 录音时放宽到 250 ms 也只是 4 帧。它和 STALE 的区别是**结论不同**：5.9 秒静默的常见
#: 原因是上位机自己的读法/调度（2026-10-14 实测撞到过），不能直接说"设备可能停了"。
SILENCE_THRESHOLD_MS = 1_000.0
#: 面板保留的样本数。100 ms 一帧，600 帧约 60 秒，够画趋势又不至于让浏览器变慢。
SAMPLE_CAPACITY = 600
#: 事件保留条数。
EVENT_CAPACITY = 200

ENVIRONMENT_TEXT = {
    "QUIET": "安静",
    "NOISY": "噪声",
    "VOICE": "人声嘈杂",
    "LOUD": "高噪声",
}

STATE_TEXT = {
    "OBSERVING": "正在感知",
    "ADJUSTING": "正在调节",
    "HOLDING": "保持当前设置",
    "RECOVERING": "正在恢复",
    "MIC FAULT": "麦克风异常",
}

#: 设备链路。README 的纪律：没有确认回执之前，不得把 simulated 说成已连接或已发送。
LINK_TEXT = {
    "simulated": "模拟",
    "connected": "已连接",
    "sent": "已发送",
}

SOURCE_TEXT = {
    "serial": "串口（真机）",
    "mock": "模拟源（无设备）",
    "replay": "回放（历史日志）",
}

#: 端侧情境模型的 9 类。中文只在这一层映射（同 state.py 顶部的纪律），固件与协议里始终是
#: 9 个 ASCII 标识。译名与 ear-context 面板（experiments/ear-context/main/ui.cpp 的
#: names[]）逐字一致：同一个模型在两块屏幕上不该有两个说法。
CONTEXT_TEXT = {
    "babble": "嘈杂人声",
    "horn": "鸣笛声",
    "music": "音乐",
    "near_voice": "近处人声",
    "other": "其他声音",
    "quiet": "安静",
    "steady": "持续噪声",
    "street": "街道声",
    "wind": "风声",
}


#: 端侧声音事件判断（main/sound_event.hpp）的类别译名。`artifact` 是输入中断（削波/撞击），
#: 不是声音类别；界面上要把它显示成"受撞击"而不是"未知"。
EVENT_TEXT = {
    "quiet": "安静",
    "speech": "说话",
    "keyboard": "敲键盘",
    "wind": "吹风",
    "artifact": "撞击/削波",
}


def event_text(value: str | None) -> str:
    """事件类别的显示文本。`None`（这一帧没报）与 `artifact` 必须分开。"""
    if value is None:
        return "—"
    return EVENT_TEXT.get(value, f"未知({value})")


def context_text(value: str | None) -> str:
    """模型类别的显示文本。

    `unknown` 单独处理：它是"窗口还没攒够 2 秒"的占位符，不是第 10 个类别。把它显示成
    "未知情境"会让人以为模型看过并给出了否定答案。
    """
    if value is None:
        return "—"
    if value == "unknown":
        return "尚未判断"
    return CONTEXT_TEXT.get(value, f"未知({value})")


def environment_text(value: str | None) -> str:
    if value is None:
        return "—"
    return ENVIRONMENT_TEXT.get(value, f"未知({value})")


def state_text(value: str | None) -> str:
    if value is None:
        return "—"
    return STATE_TEXT.get(value, f"未知({value})")


def link_text(value: str | None) -> str:
    if value is None:
        return "—"
    return LINK_TEXT.get(value, f"未知({value})")


def capture_text(value: str | None) -> str:
    return {
        None: "未在录音",
        "started": "录音中",
        "stopped": "录音已停止",
        "finished": "录音已完成",
        "encode_failed": "录音编码失败",
    }.get(value, f"未知({value})")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return ordered[index]


@dataclass
class Event:
    """状态变化或值得回看的一刻。"""

    at: float
    kind: str
    text: str
    detail: str = ""

    def as_json(self, origin: float) -> dict[str, Any]:
        return {
            "at": round(self.at - origin, 2),
            "kind": self.kind,
            "text": self.text,
            "detail": self.detail,
        }


@dataclass
class Problem:
    at: float
    text: str

    def as_json(self, origin: float) -> dict[str, Any]:
        return {"at": round(self.at - origin, 2), "text": self.text}


@dataclass
class Stats:
    commands: int = 0
    gaps: int = 0
    frames_lost: int = 0
    max_gap: int = 0
    latency_reports: int = 0
    latency_frames: int = 0
    chunks: int = 0
    chunks_lost: int = 0
    bad_lines: int = 0
    lines: int = 0
    unparsed_json: int = 0
    intervals_ms: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    #: 超过阈值的到达间隔。**不能只丢掉了事**：旧版把 ≥5 秒的间隔直接过滤掉，于是"上位机
    #: 自己跟丢了 5.9 秒"在面板上完全看不见（max 仍显示 101 ms）——而那正是最该被看见的
    #: 一类故障，2026-10-14 用户实测撞到过。
    silences_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))
    max_silence_ms: float = 0.0

    def interval_stats(self) -> dict[str, float]:
        values = list(self.intervals_ms)
        if not values:
            return {"p50": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "n": 0}
        return {
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "min": min(values),
            "max": max(values),
            "n": len(values),
        }


class HostState:
    """把设备行的流归约成上位机要显示的状态。

    线程安全约定：**只有数据源线程调用 feed_*，只有它和 HTTP 线程调用 snapshot()。**
    snapshot() 会构造一份完整的普通字典，因此读出之后不再与内部状态共享可变对象——
    界面线程不会读到一半被改掉的数据。
    """

    def __init__(self, source: str = "serial", detail: str = "", capacity: int = SAMPLE_CAPACITY,
                 on_mic_chunk: Callable[[MicChunk], None] | None = None):
        self.started_at = time.monotonic()
        self.source = source
        self.source_detail = detail
        self.capacity = capacity
        self.on_mic_chunk = on_mic_chunk

        self.connected = False
        self.connected_detail = ""
        #: 端口打开的时刻。没有它，"从未收到过帧"就没有起点，只能靠 `connected` 硬判 stale
        #: （旧行为的 bug：一打开面板就报"已连接但没有帧"，而 age 处只能显示 "?"）。
        self.connected_at: float | None = None
        self.last_frame_at: float | None = None
        #: Mac 播放与执行端（tools/player）。为 None 表示本次运行没有启用它。
        self.player = None
        #: 音频中继（S3 麦克风 → Mac 输出）。为 None = 本次运行没开中继。
        self.relay = None
        #: 环境声执行端（tools/host/ambient.py，切 AirPods 聆听模式）。为 None = 没启用。
        self.ambient = None

        self.latest: CommandFrame | None = None
        self.latency: LatencyReport | None = None
        #: WavLM 声纹影子门控（tools/host/voiceprint.py）。None = 本次没启用。
        self.voiceprint: object | None = None
        self.latency_at: float | None = None

        self.capture_state: str | None = None
        self.capture_seconds: int | None = None
        self.capture_rate: int | None = None
        self.capture_finished_chunks: int | None = None
        #: 设备在录音期间每 5 秒报一次的内诊断（原样保留，界面负责翻译显示）。
        self.capture_progress: dict[str, int] | None = None
        self.capture_progress_at: float | None = None
        self.audio_samples: int = 0
        self.recording = False

        # 回控应答追踪。设备侧只实现了 rec start / rec stop 两条命令，而"写成功了"与
        # "设备照做了"是两件事：实测这台 CoreS3 上 write() 返回成功、设备却没有任何反应
        # （控制台输入没有接到 USB-Serial/JTAG）。界面必须区分这两件事。
        #: 串口发生过多少次瞬时错误后的重连。静默重连与不重连一样糟：看不出来中间断过。
        self.read_errors = 0
        self.reconnects = 0

        self.control_sent = 0
        self.control_acknowledged = 0
        self.last_control: str | None = None
        self.last_control_at: float | None = None
        self.last_capture_at: float | None = None

        self.stats = Stats()
        self.samples: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.events: deque[Event] = deque(maxlen=EVENT_CAPACITY)
        self.problems: deque[Problem] = deque(maxlen=EVENT_CAPACITY)
        self.raw_tail: deque[str] = deque(maxlen=12)

        # 数据源线程写、HTTP 线程读。snapshot() 会遍历几个 deque，遍历时被改会抛
        # RuntimeError，所以读写都用同一把锁——界面崩掉比慢一点糟得多。
        self.lock = threading.RLock()
        self._expected_sequence: int | None = None
        self._expected_chunk: int | None = None
        self._last_state: str | None = None
        self._last_environment: str | None = None
        self._last_context: str | None = None
        self._last_event: str | None = None
        self._last_mic_valid: bool | None = None

    # ------------------------------------------------------------------ 连接

    def set_source_detail(self, detail: str) -> None:
        """数据源在 run() 里才解析出实际端口，所以详情要在那之后回填。

        这一格不是装饰：端口被占用后自动换端口、或在多个候选里挑了哪一个，只有这里能看到。
        """
        with self.lock:
            self.source_detail = detail

    def set_connection(self, connected: bool, detail: str = "") -> None:
        with self.lock:
            self._set_connection(connected, detail)

    def _set_connection(self, connected: bool, detail: str) -> None:
        if connected != self.connected or detail != self.connected_detail:
            self.connected = connected
            self.connected_detail = detail
            # 连接的起点要记下来：设备插着但一帧都没来时，"等了多久"只能从这一刻算。
            self.connected_at = time.monotonic() if connected else None
            self.log("link", "上位机已连接设备" if connected else "上位机与设备断开", detail)

    def attach_relay(self, relay: object | None) -> None:
        """挂上音频中继（tools/host/audio_relay.py）。

        与 attach_player 同样的鸭子类型理由：状态层要能在没有音频设备的环境里单独跑。
        中继的数字是**播放端自己报出来的实测值**（排队深度、丢弃量、驱动侧输出延迟），
        面板显示它们而不是显示我们猜的值——这正是这条通路上原来最缺的东西。
        """
        with self.lock:
            self.relay = relay

    def attach_player(self, player: object | None) -> None:
        """挂上 Mac 播放与执行端（tools/host/player.py）。

        用鸭子类型而不是 import：state.py 要能在没有播放器的环境里单独跑（主机单测就是
        这么用它的），不希望因为一个音频模块把整个状态层拖下水。
        """
        with self.lock:
            self.player = player

    def attach_voiceprint(self, voiceprint: object | None) -> None:
        """挂上声纹影子门控。只进快照，不参与任何决策。"""
        with self.lock:
            self.voiceprint = voiceprint

    def attach_ambient(self, ambient: object | None) -> None:
        """挂上环境声执行端（tools/host/ambient.py）。

        与 player/relay 同样的鸭子类型理由。这里必须把两类信息分开：
        **设备意图**（帧里的 `ambient_open`）与**执行结果**（`ambient.open`）。
        "意图 true、执行失败"是一个必须被看见的状态，不能合并成"已开放"。
        """
        with self.lock:
            self.ambient = ambient

    # ------------------------------------------------------------------ 喂数据

    def feed_line(self, line: str) -> None:
        """处理一行串口输出。"""
        with self.lock:
            self._feed_line(line)

    def _feed_line(self, line: str) -> None:
        self.stats.lines += 1
        parsed = parse_line(line)
        if parsed.value is None and parsed.error is None:
            # 非协议文本：ESP-IDF 的日志横幅等。保留最后几行原文供现场排查。
            text = line.strip()
            if text:
                self.raw_tail.append(text[:160])
            return
        if parsed.error is not None:
            self.stats.bad_lines += 1
            if parsed.json_line:
                self.stats.unparsed_json += 1
            self.raw_tail.append(line.strip()[:160])
            self.note(f"无法解析的一行：{parsed.error.reason}")
            return

        if parsed.problems:
            # 告警只在这里发一次。放在各 feed_* 里会让同一帧的异常被报两遍
            # （feed_line 报一次、feed_* 再报一次），异常列表会立刻失去可读性。
            self.note("；".join(parsed.problems))

        value = parsed.value
        if isinstance(value, CommandFrame):
            self.feed_command(value, parsed.problems)
        elif isinstance(value, LatencyReport):
            self.feed_latency(value)
        elif isinstance(value, CaptureEvent):
            self.feed_capture(value)
        elif isinstance(value, MicChunk):
            self.feed_chunk(value)
        elif isinstance(value, RouteEvent):
            self.log("route", "设备确认 Mac 音频路由", value.device or value.state)

    def feed_command(self, frame: CommandFrame, problems: tuple[str, ...] = ()) -> None:
        now = time.monotonic()
        self.stats.commands += 1

        if self._expected_sequence is not None and frame.sequence != self._expected_sequence:
            lost = frame.sequence - self._expected_sequence
            if lost > 0:
                self.stats.gaps += 1
                self.stats.frames_lost += lost
                self.stats.max_gap = max(self.stats.max_gap, lost)
                self.note(
                    f"控制帧序号跳变：{self._expected_sequence} → {frame.sequence}，"
                    f"丢了 {lost} 帧（USB 丢数据，曲线会出现断口）"
                )
            else:
                self.note(f"控制帧序号回退：{self._expected_sequence} → {frame.sequence}（设备重启过？）")
                self._expected_sequence = frame.sequence
        self._expected_sequence = frame.sequence + 1

        if self.last_frame_at is not None:
            interval_ms = (now - self.last_frame_at) * 1000.0
            if 0 < interval_ms < SILENCE_THRESHOLD_MS:
                self.stats.intervals_ms.append(interval_ms)
            elif interval_ms >= SILENCE_THRESHOLD_MS:
                # 静默单独记账：它不属于"节拍分布"（会把 p50 拖坏），更不能被丢掉——
                # 丢掉就等于面板看不见"自己跟丢了多久"。
                self.stats.silences_ms.append(interval_ms)
                self.stats.max_silence_ms = max(self.stats.max_silence_ms, interval_ms)
        self.last_frame_at = now
        self.latest = frame

        self.samples.append(
            {
                "seq": frame.sequence,
                "t": round(now - self.started_at, 3),
                "ts": frame.timestamp_ms,
                "g": frame.speech_gain_db,
                "vg": frame.external_voice_gain_db,
                "n": frame.noise_reduction_db,
                "m": frame.media_percent,
                "c": frame.confidence,
                "env": frame.environment,
                "state": frame.state,
                "mode": frame.mode,
                "conversation": frame.conversation,
                # 情境类别（模型的判断）。与 env（固件自己的四分类）是两个不同的量，
                # 并排看它们的**分歧**才是这个模型现阶段的用处。窗没满时为 None。
                "ctx": frame.context if frame.context_valid else None,
                # 声音事件（1 s 窗规则）。valid=false 时给 None，与"判断为安静"分开。
                "evt": frame.event if frame.event_valid else None,
            }
        )

        if frame.state != self._last_state:
            self.log("state", f"动作 → {state_text(frame.state)}", frame.state)
            self._last_state = frame.state
        if frame.environment != self._last_environment:
            self.log("environment", f"环境 → {environment_text(frame.environment)}", frame.environment)
            self._last_environment = frame.environment
        # 情境只在**有效**时才记变更。预热期（窗没满）每一帧都是 unknown，若按它记变更，
        # 开机会刷出一串"情境 → 尚未判断"，把真正的那次变化埋掉。
        if frame.context_valid and frame.context != self._last_context:
            self.log(
                "context",
                f"情境 → {context_text(frame.context)}（模型 {frame.context_score:.2f}）",
                frame.context,
            )
            self._last_context = frame.context

        # 事件只在有效时记变更；quiet 不刷事件，否则一次敲击结束会立刻刷一条"安静"。
        # artifact 单独记：它表示输入中断（撞击/削波），是排查"模型 2 秒没输出"的钥匙。
        if frame.event_valid and frame.event != self._last_event:
            if frame.event in ("keyboard", "wind", "speech", "artifact"):
                self.log("event", f"事件 → {event_text(frame.event)}", frame.event)
            self._last_event = frame.event

        # 麦克风有效性没有单独字段，只能从 state 判断（固件在 mic_valid 为假时进 mic_fault）。
        mic_valid = frame.state != "MIC FAULT"
        if self._last_mic_valid is not None and mic_valid != self._last_mic_valid:
            self.log(
                "mic",
                "麦克风恢复正常" if mic_valid else "麦克风异常（固件报 MIC FAULT）",
                "",
            )
        self._last_mic_valid = mic_valid

    def feed_latency(self, report: LatencyReport) -> None:
        self.stats.latency_reports += 1
        self.stats.latency_frames += report.frames
        self.latency = report
        self.latency_at = time.monotonic()

    def note_read_error(self, detail: str) -> None:
        self.read_errors += 1
        self.note(
            f"串口读取出错第 {self.read_errors} 次：{detail}"
            "（常见原因是别的进程也在开这个串口；上位机会自动重连）"
        )

    def note_reconnect(self, detail: str = "") -> None:
        self.reconnects += 1
        self.log("link", f"串口已重连（第 {self.reconnects} 次）", detail)

    def note_control_sent(self, command: str) -> None:
        self.control_sent += 1
        self.last_control = command
        self.last_control_at = time.monotonic()
        self.log("control", f"已向设备发送：{command}", "")

    def note_control_result(self, acknowledged: bool, detail: str = "") -> None:
        if acknowledged:
            self.control_acknowledged += 1
            self.log("control", "设备已回应", detail)
        else:
            self.note(
                f"命令 {self.last_control} 已写入串口，但设备没有任何回应（{detail}）。"
                "写成功不等于设备照做——当前症状与控制台输入未接到 USB-Serial/JTAG 一致。"
            )

    def feed_capture(self, event: CaptureEvent) -> None:
        self.last_capture_at = time.monotonic()
        if event.state == "started":
            self.recording = True
            self.capture_state = "started"
            self.capture_seconds = event.seconds
            self.capture_rate = event.rate or CAPTURE_RATE_HZ
            self.audio_samples = 0
            self._expected_chunk = None
            self.stats.chunks = 0
            self.stats.chunks_lost = 0
            duration = (
                "直到 rec stop" if not event.seconds else f"{event.seconds} 秒"
            )
            self.log("capture", f"设备开始录音（{duration}）", f"{self.capture_rate} Hz")
            self.note("设备进入录音状态：控制节拍放宽到 250 ms，属预期行为")
        elif event.state == "stopped":
            self.recording = False
            self.capture_state = "stopped"
            self.log("capture", "设备录音已停止", f"共 {self.stats.chunks} 块")
        elif event.state == "finished":
            self.recording = False
            self.capture_state = "finished"
            self.capture_finished_chunks = event.chunks
            self.log(
                "capture",
                "设备录音完成",
                f"{self.stats.chunks} 块 / {self.audio_samples / self.capture_rate:.2f} s"
                if self.capture_rate
                else "",
            )
        elif event.state == "progress":
            # 设备自报的录音通路计数。这不是给最终用户看的数字，而是"录音为什么不满"
            # 的唯一现场证据：上位机只负责原样呈现，不做解释。
            self.capture_progress = dict(event.progress or {})
            self.capture_progress_at = time.monotonic()
        elif event.state == "encode_failed":
            self.recording = False
            self.capture_state = "encode_failed"
            self.note(f"设备录音编码失败（{event.samples} 点）")

    def feed_chunk(self, chunk: MicChunk) -> None:
        self.stats.chunks += 1
        if chunk.sequence is not None:
            if self._expected_chunk is not None and chunk.sequence != self._expected_chunk:
                lost = chunk.sequence - self._expected_chunk
                if lost > 0:
                    self.stats.chunks_lost += lost
                    # 把最可能的成因一起说出来。真机踩过两次：这个现象的根因是**另一个进程
                    # 也在读同一个串口**，两边互相偷走对方的字节——一次是忘了停旧的上位机，
                    # 一次是诊断脚本自己开了端口。只报"有断裂"会让人去查 USB 带宽、固件丢块、
                    # 蓝牙干扰，而真正的处置只是关掉那个抢占者。
                    self.note(
                        f"录音块缺失 {lost} 块：这一段录音有断裂，不能用于声纹或信噪比结论。"
                        "最常见的原因是另一个进程也在读同一个串口（旧的上位机没停，"
                        "或诊断脚本自己开了这个端口）；先确认只有一个读者，再怀疑带宽或固件。"
                    )
            self._expected_chunk = chunk.sequence + 1
        self.audio_samples += chunk.sample_count
        if self.on_mic_chunk is not None:
            self.on_mic_chunk(chunk)

    # ------------------------------------------------------------------ 记录

    def log(self, kind: str, text: str, detail: str = "") -> None:
        self.events.append(Event(time.monotonic(), kind, text, detail))

    def note(self, text: str) -> None:
        self.problems.append(Problem(time.monotonic(), text))

    # ------------------------------------------------------------------ 读出

    def silence_ms(self, now: float | None = None) -> float | None:
        """距离上一次收到控制帧多久。一帧都还没收到时，从**连接那一刻**算。

        旧实现分叉成两套判据，于是自相矛盾：没有帧时 `is_stale()` 直接返回 `connected`
        （一打开面板就报"已连接但没有帧"，而 `age_ms()` 偏偏返回 None，界面只能显示 "?"）；
        有帧时又要求超过 10 秒。改成同一个起点之后，两个字段永远一致，告警里也永远有数字。
        """
        now = now if now is not None else time.monotonic()
        if self.last_frame_at is not None:
            return (now - self.last_frame_at) * 1000.0
        if self.connected_at is not None:
            return (now - self.connected_at) * 1000.0
        return None

    def is_stale(self, now: float | None = None) -> bool:
        """连接中且超过 STALE_TIMEOUT_MS 没有帧——这时才敢说"设备可能停了"。"""
        value = self.silence_ms(now)
        return bool(self.connected and value is not None and value > STALE_TIMEOUT_MS)

    def is_silent(self, now: float | None = None) -> bool:
        """连接中且静默超过 SILENCE_THRESHOLD_MS，但还没到 STALE。

        与 `is_stale` **分开报**，因为结论不同：几秒的静默常见原因是上位机自己的读法或
        调度（文档里记过：pyserial 的 read 粒度、2026-10-14 实测 5.9 秒静默），
        直接说"设备可能停了"就是把读者引到错误的排查方向。
        """
        value = self.silence_ms(now)
        return bool(self.connected and value is not None and value >= SILENCE_THRESHOLD_MS)

    def age_ms(self, now: float | None = None) -> float | None:
        value = self.silence_ms(now)
        return None if value is None else round(value, 1)

    def snapshot(self) -> dict[str, Any]:
        """构造给界面的一份完整状态。不返回内部对象，避免界面读到一半被改。"""
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        frame = self.latest
        latency = self.latency
        origin = self.started_at

        return {
            "source": self.source,
            "source_text": SOURCE_TEXT.get(self.source, self.source),
            "source_detail": self.source_detail,
            "read_errors": self.read_errors,
            "reconnects": self.reconnects,
            "uptime_s": round(now - origin, 1),
            "connected": self.connected,
            "connected_detail": self.connected_detail,
            "age_ms": self.age_ms(now),
            "stale": self.is_stale(now),
            # 静默与断流分开报：前者常见于上位机自己跟丢了，后者才指向设备。
            "silent": self.is_silent(now),
            "silence_threshold_ms": SILENCE_THRESHOLD_MS,
            "recording": self.recording,
            #: Mac 播放与执行端的自报状态。为 None = 本次运行没启用播放器。
            "player": self.player.snapshot() if self.player is not None else None,
            #: 中继的实时读数。measurable=False 表示这个播放端不报告排队深度——
            #: 界面必须把"测不到"和"延迟为零"分开显示。
            "relay": self.relay.live() if self.relay is not None else None,
            #: 环境声执行端（AirPods 聆听模式）。None = 本次运行没启用执行端；
            #: 设备意图在 frame.ambient_open，两者必须分开读。
            "ambient": self.ambient.snapshot() if self.ambient is not None else None,
            "voiceprint": self.voiceprint.snapshot() if self.voiceprint is not None else None,
            "control": {
                "sent": self.control_sent,
                "acknowledged": self.control_acknowledged,
                "last": self.last_control,
                "last_at": None
                if self.last_control_at is None
                else round(self.last_control_at - origin, 1),
                # 发过命令但一次都没等到回应 → 界面要明确说这是降级，而不是让按钮
                # 看起来"点过了"。判定标准是"设备是否进过录音态"，因为那是命令被消费
                # 的唯一可观测证据。
                # A capture event is itself device-side proof that rec start was
                # consumed. The automatic relay command does not pass through the
                # HTTP acknowledgement waiter, so requiring that counter alone
                # produced a false "device never entered recording" alert.
                "unresponsive": (
                    self.control_sent > 0
                    and self.control_acknowledged == 0
                    and self.last_capture_at is None
                ),
                "last_capture_at": None
                if self.last_capture_at is None
                else round(self.last_capture_at - origin, 1),
            },
            "capture": {
                "state": self.capture_state,
                "text": capture_text(self.capture_state),
                "seconds": self.capture_seconds,
                "rate": self.capture_rate,
                "chunks": self.stats.chunks,
                "chunks_lost": self.stats.chunks_lost,
                "audio_seconds": round(self.audio_samples / self.capture_rate, 2)
                if self.capture_rate
                else 0.0,
                "finished_chunks": self.capture_finished_chunks,
                "progress": self.capture_progress,
                "progress_age_s": None
                if self.capture_progress_at is None
                else round(now - self.capture_progress_at, 1),
            },
            "frame": None
            if frame is None
            else {
                "sequence": frame.sequence,
                "timestamp_ms": frame.timestamp_ms,
                "speech_gain_db": frame.speech_gain_db,
                "external_voice_gain_db": frame.external_voice_gain_db,
                "media_percent": frame.media_percent,
                # v4：环境声开放**意图**（不是执行回执）。None = 这一帧没报（旧固件）。
                "ambient_open": frame.ambient_open,
                # v4 判据遥测（训练对话预判/排查误触发）。None = 这一帧没报。
                "broadband_dbfs": frame.broadband_dbfs,
                "voice_likelihood": frame.voice_likelihood,
                "vocal_ratio": frame.vocal_ratio,
                "spectral_flatness": frame.spectral_flatness,
                "vocal_peakiness": frame.vocal_peakiness,
                "excited_vocal_bands": frame.excited_vocal_bands,
                "confidence": frame.confidence,
                "state": frame.state,
                "state_text": state_text(frame.state),
                "environment": frame.environment,
                "environment_text": environment_text(frame.environment),
                "noise_spectrum": frame.noise_spectrum,
                # 端侧情境模型。四个字段整体为 None 表示这一帧没报（融合第二轮之前的固件），
                # `valid=false` 表示报了、但窗口还没攒够——界面必须把这两件事分开显示。
                "own_voice_confidence": frame.own_voice_confidence,
                "own_voice_correlation": frame.own_voice_correlation,
                "own_voice_source": frame.own_voice_source,
                "evidence_policy": frame.evidence_policy,
                "imu_gravity": frame.imu_gravity,
                "imu_ok": frame.imu_ok,
                "imu_motion": frame.imu_motion,
                "imu_drops": frame.imu_drops,
                "imu_samples": frame.imu_samples,
                "context": frame.context,
                "context_text": context_text(frame.context),
                "context_score": frame.context_score,
                "context_valid": frame.context_valid,
                "context_us": frame.context_us,
                # 声音事件判断。与情境模型同一纪律：没报 / 窗未满 / 有判断三态分开。
                "event": frame.event,
                "event_text": event_text(frame.event),
                "event_valid": frame.event_valid,
                # 说话/非说话二分类学生：只做遥测，界面要标"只显示"。
                "speech_gate": frame.speech_gate,
                "speech_gate_valid": frame.speech_gate_valid,
                "speech_gate_us": frame.speech_gate_us,
                "mode": frame.mode,
                "conversation": frame.conversation,
                "execution": frame.execution,
                "ambient_band_dbfs": list(frame.ambient_band_dbfs),
                "link": frame.link,
                "link_text": link_text(frame.link),
                "link_reported_connected": frame.link_reported_connected,
                # 调音：设备**实际**要施加的每频段增益。固件 2026-10-14 起把 eq_gain_db
                # 发进 JSON，所以这里是设备报回来的值；旧固件不发时为 None，界面会
                # 如实说"这一帧没有调音数据"，而不是拿标量增益去推一个形状。
                "tuning": None
                if frame.tuning is None
                else {
                    "band_gain_db": list(frame.tuning.band_gain_db),
                    "lifted_bands": frame.tuning.lifted_bands,
                    "peak_gain_db": frame.tuning.peak_gain_db,
                    "shape_is_speech_band": frame.tuning.shape_is_speech_band,
                },
                "schema": frame.schema_version,
            },
            #: 只保留本产品真正有动作的上限。固件的降噪硬上限不出现在界面上。
            #: speech_gain_db 用**实发**上限（8.0）：面板的增益条按它画，按 6.5 画会把
            #: 8 dB 的正常帧画成满格越界。产品策略值单独给一个字段，供界面注明出处。
            "caps": {
                "speech_gain_db": DEMO_SPEECH_GAIN_DB,
                "policy_speech_gain_db": MAX_SPEECH_GAIN_DB,
            },
            "latency": None
            if latency is None
            else {
                "frames": latency.frames,
                "total_p50_ms": latency.total_p50_ms,
                "total_p95_ms": latency.total_p95_ms,
                "overhead_mean_us": latency.overhead_mean_us,
                "overhead_max_us": latency.overhead_max_us,
                # 两个端侧小模型的耗时也带出来：它们在 stages 之外，但同样是实时成本。
                "context_p50_us": latency.context_p50_us,
                "event_p50_us": latency.event_p50_us,
                "age_s": round(now - (self.latency_at or now), 1),
                "stages": [
                    {
                        "stage": item.stage,
                        "n": item.samples,
                        "p50_ms": item.p50_ms,
                        "p95_ms": item.p95_ms,
                        "max_ms": item.max_ms,
                        # local 决定界面怎么解释"0"：本地阶段 0 值＝未实现，
                        # 对端阶段 0 样本＝对端没接通。两者不能混为一谈。
                        "local": item.local,
                    }
                    for item in latency.stages
                ],
            },
            "stats": {
                "commands": self.stats.commands,
                "gaps": self.stats.gaps,
                "frames_lost": self.stats.frames_lost,
                "max_gap": self.stats.max_gap,
                "latency_reports": self.stats.latency_reports,
                "latency_frames": self.stats.latency_frames,
                "lines": self.stats.lines,
                "bad_lines": self.stats.bad_lines,
                "unparsed_json": self.stats.unparsed_json,
                # 静默单独一组：进不了节拍分布（会拖坏 p50），但也绝不能消失。
                "silences": len(self.stats.silences_ms),
                "max_silence_ms": round(self.stats.max_silence_ms, 1),
                "last_silence_ms": round(self.stats.silences_ms[-1], 1)
                if self.stats.silences_ms
                else None,
                "interval": self.stats.interval_stats(),
            },
            "samples": list(self.samples),
            "events": [item.as_json(origin) for item in self.events],
            "problems": [item.as_json(origin) for item in self.problems],
            "raw_tail": list(self.raw_tail),
        }


def self_check() -> list[str]:
    """返回本模块的静态自检项，供测试断言"枚举映射没有漏项"。

    固件新增一个状态而这里没跟上，面板会显示 `未知(XXX)`——那是**看得见的错**，
    比静默显示旧词安全。这个函数把两份清单绑在一起，让漏项在单测里就暴露。
    """
    notes: list[str] = []
    for name in KNOWN_STATES:
        if name not in STATE_TEXT:
            notes.append(f"缺少状态中文映射: {name}")
    for name in KNOWN_ENVIRONMENTS:
        if name not in ENVIRONMENT_TEXT:
            notes.append(f"缺少环境中文映射: {name}")
    # 模型的 9 个类别同理：固件换了类别表而这里没跟上，面板会显示 `未知(horn)`——看得见，
    # 但不如在单测里就红。
    for name in CONTEXT_CLASSES:
        if name not in CONTEXT_TEXT:
            notes.append(f"缺少情境类别中文映射: {name}")
    return notes


__all__ = [
    "CONTROL_PERIOD_MS",
    "CAPTURE_PERIOD_MS",
    "CONTEXT_TEXT",
    "ENVIRONMENT_TEXT",
    "LINK_TEXT",
    "SOURCE_TEXT",
    "STATE_TEXT",
    "Event",
    "HostState",
    "ParseError",
    "Problem",
    "Stats",
    "capture_text",
    "context_text",
    "event_text",
    "environment_text",
    "link_text",
    "self_check",
    "state_text",
]
