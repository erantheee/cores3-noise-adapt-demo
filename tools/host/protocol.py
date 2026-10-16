"""CoreS3 串口协议解析。

设备从 USB 串口按行输出 JSON，本项目此前已有两个消费者：
`tools/record_from_device.py`（只认 mic_chunk）与 `tools/drift_capture.py`（只认
earbud_command）。上位机是第三个，而且它必须同时处理**全部四类**行：

    earbud_command   每 ~100 ms 一帧的控制指令
    latency          每 10 s 一次的逐阶段延迟汇总（main/app_main.cpp: emit_latency_report）
    capture          录音状态变化（started / stopped / finished / encode_failed）
    mic_chunk        录音数据块，16 kHz 单声道 base64（main/app_main.cpp: emit_capture_chunk）

本模块**不依赖 pyserial，也不做任何 IO**，因此可以在开发机上秒级自测。凡是解析规则
都必须在这里落地，不要散落到串口读取或界面代码里——那正是"改了设备侧却没人发现上位机
已经读不懂"的来源。

解析的一条纪律：**校验上限，而不只是校验类型。** 固件侧有人声增益 +6.5 dB、降噪 −8 dB
的硬上限（main/controller.hpp: kMaxSpeechGainDb / kMaxNoiseReductionDb）。上位机是演示
现场唯一会把这些数字讲给人听的地方，所以一旦收到越界的值，必须**标记为异常并保留原始
值**，而不是默默钳掉——钳掉会把"固件越界"伪装成"一切正常"。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

#: 本上位机实现的协议版本。设备侧 schema_version 不是这个值时必须显式报告不兼容，
#: 而不是按 v1 去解读——字段含义变了却继续显示，比显示不出来更糟。
SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3)

#: 固件侧的**产品策略**上限（main/controller.hpp 的 kMaxSpeechGainDb）。
#: 注意：固件实际下发的值被钳到 demo 上限，不是这一个——见下面那个常量。
MAX_SPEECH_GAIN_DB = 6.5
#: 固件**实际下发**的上限（main/controller.hpp 的 kDemoSpeechGainDb，末尾
#: `std::clamp(speech_gain_db_, 0.0f, kDemoSpeechGainDb)`）。校验必须用它：
#: 按 6.5 校验会把每一帧正常的 8 dB 帧标成"超出硬上限"，把一个正常动作报成异常。
DEMO_SPEECH_GAIN_DB = 8.0
MAX_NOISE_REDUCTION_DB = 8.0

#: 固件侧中继输出参数，与 kRelayRate / kCaptureChunkSamples 一致。
CAPTURE_RATE_HZ = 16000
CAPTURE_CHUNK_SAMPLES = 320

#: 已知的固件状态与环境的字符串（main/controller.cpp 的 adapt_state_name /
#: environment_name）。未知值要如实显示为"未知(原文)"，不要猜。
KNOWN_STATES = ("OBSERVING", "ADJUSTING", "HOLDING", "RECOVERING", "MIC FAULT")
KNOWN_ENVIRONMENTS = ("QUIET", "NOISY", "VOICE", "LOUD")

#: 端侧听音情境模型的 9 个类别，顺序就是 main/listening_model.h 里的类别下标顺序
#: （export_c.py / `kClassNames`）。融合第二轮（2026-10-16）起固件在 `earbud_command`
#: 里报 `context` / `context_score` / `context_valid` / `context_us`；旧固件不发这四个字段。
#:
#: 这 9 个字符串**不是**上位机可以改的显示文本，它们同时是模型输出的标识：翻译成中文
#: 只发生在 state.py 的 `context_text()`，别在这里做映射。
CONTEXT_CLASSES = (
    "babble",
    "horn",
    "music",
    "near_voice",
    "other",
    "quiet",
    "steady",
    "street",
    "wind",
)

#: `context_valid` 为 false 时固件报的占位类别（窗口还没攒够 2 秒，或输入刚被打断）。
#: 它**不在**上面那 9 类里：把"还没判断"和"判断为 unknown 情境"混为一谈，界面就会
#: 把预热期显示成一个结论。
CONTEXT_UNKNOWN = "unknown"


#: 端侧自己测量的阶段。其余阶段由对端上报（本机目前没有对端，所以它们的 n 一直是 0）。
#: 与 main/latency_profiler.hpp 的 Stage 枚举顺序一致。
LOCAL_STAGES = frozenset({"capture", "analyse", "embed", "decide"})


def d2(value: float) -> float:
    """按 C 的 `%.2f` 语义取两位小数（四舍五入，而不是 Python 的银行家舍入）。

    固件用 printf 给出两位小数，设备屏幕与串口因此是两个数。上位机要显示的是**设备实际
    报出的那一位**：0.125 在 C 里是 0.13，在 Python 里 round() 会给出 0.12。差 0.01 dB
    不影响结论，但会让"面板数字"和"日志数字"对不上，而对不上就会被当成两个不同的测量。
    """
    return float(Decimal(repr(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Tuning:
    """24 频段调音：设备实际要施加的每频段增益。"""

    band_gain_db: tuple[float, ...]
    #: 被抬起的频段数（>0.05 dB 才算）。
    lifted_bands: int
    #: 抬起的幅度（各频段里的最大值）。
    peak_gain_db: float

    @property
    def shape_is_speech_band(self) -> bool:
        """是否就是"只抬语音段、其余 0 dB"这一个形状。

        当前固件只有这一种形状（main/controller.cpp 的 eq_gain_db 循环）。写成性质判断
        而不是硬编码 8–16，是为了将来固件真的做多段整定时，这里不会跟着说谎。
        """
        lifted = [index for index, value in enumerate(self.band_gain_db) if value > 0.05]
        if not lifted:
            return True
        return lifted == list(range(min(lifted), max(lifted) + 1))


@dataclass(frozen=True)
class CommandFrame:
    """一帧 EarbudCommand；v1 历史日志与当前 v2 都可读取。"""

    sequence: int
    timestamp_ms: int
    speech_gain_db: float
    media_percent: int
    state: str
    environment: str
    confidence: float
    link: str
    external_voice_gain_db: float = 0.0
    mode: str = "LEGACY"
    conversation: str = "NONE"
    execution: str = "decision_only"
    ambient_band_dbfs: tuple[float, ...] = ()
    #: 旧固件的字段（已从固件删除）。保留只为读得懂历史日志；上位机不显示它。
    noise_reduction_db: float | None = None
    #: 设备实际要施加的调音。None 表示这一帧没有报（旧固件不发 eq_gain_db）。
    tuning: Tuning | None = None
    #: 端侧情境模型（v0.2-s3）的输出。None 表示这一帧没有报这四个字段（融合第二轮之前的
    #: 固件）。`context_valid=False` 是"窗口还没攒够 2 秒"，不是"模型说是 unknown"。
    context: str | None = None
    context_score: float | None = None
    context_valid: bool | None = None
    #: 一次前向的耗时，微秒。设备逐帧实报，不是估算。
    context_us: int | None = None
    #: 自身语音证据（v3）。None 表示这一帧没报——v3 之前的固件不发这几个字段。
    #: source 有三种：`bone`（骨传导 IMU）、`near_field`（调用方显式打开开关后的代理兜底）、
    #: `none`（没有证据，fail-closed 不动作）。三态必须分开：把 none 显示成 near_field，
    #: 看的人会以为代理在跑。
    own_voice_confidence: float | None = None
    own_voice_correlation: float | None = None
    own_voice_source: str | None = None
    #: IMU 重力基线模长（m/s²）。静止时 ≈9.81 才说明量程与单位对；0 表示没有 IMU 数据。
    imu_gravity: float | None = None
    #: IMU 这一拍是否有数，以及运动否决是否生效。None = 这一帧没报。
    imu_ok: bool | None = None
    imu_motion: bool | None = None
    #: IMU 队列累计丢弃数（诊断计数）。它是"与音频任务同跑会不会丢采样"的直接证据：
    #: 真机验证看的是它在整个验证过程里有没有增长，而不是某一帧的绝对值。
    imu_drops: int | None = None
    schema_version: int = SCHEMA_VERSION
    #: 越界或无法解释的字段说明。空表示这一帧通过了全部校验。
    problems: tuple[str, ...] = ()

    @property
    def link_reported_connected(self) -> bool:
        """设备自称链路已连。

        只有 `link` 不再是 `simulated` 时才为真。README 的纪律：没有确认回执前不得把
        simulated 说成已连接或已发送。这个属性只反映**设备说了什么**，上位机自己的
        串口连接是另一回事（见 state.HostState.connected）。
        """
        return self.link != "simulated"


@dataclass(frozen=True)
class StageStat:
    stage: str
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    #: True 表示这一阶段是端侧自己测的（stage 0–3），False 表示要由对端上报（4–6）。
    #: 依据 main/latency_profiler.hpp 的 is_local_stage / 注释「Stages 4-6 are reported
    #: by the peer」。界面必须靠这个字段区分两种"数字是 0"：
    #:   · embed n=68 而 p50=0.000 → 固件每帧 record(embed, 0.0f)，是**未实现**（本地阶段）
    #:   · transport/apply/render n=0 → 对端还没接通，**不是未实现**，是没样本
    #: 按 n==0 去标"未实现"会正好标反：把没接通说成没实现，把真没实现的显示成实测 0 ms。
    local: bool = True


@dataclass(frozen=True)
class LatencyReport:
    frames: int
    total_p50_ms: float
    total_p95_ms: float
    overhead_mean_us: float
    overhead_max_us: float
    overhead_samples: int
    stages: tuple[StageStat, ...] = ()

    def stage(self, name: str) -> StageStat | None:
        for item in self.stages:
            if item.stage == name:
                return item
        return None


@dataclass(frozen=True)
class CaptureEvent:
    state: str
    seconds: int | None = None
    rate: int | None = None
    chunks: int | None = None
    samples: int | None = None
    #: 录音期间每 5 秒一次的设备内诊断（state == "progress"）。键值随固件演进，
    #: 所以按原样带出来给界面显示，而不是每个字段都建一个成员——固件加一个计数器时
    #: 上位机不该因为不认识就把它丢掉。
    progress: dict[str, int] | None = None


@dataclass(frozen=True)
class RouteEvent:
    state: str
    device: str | None = None


@dataclass(frozen=True)
class MicChunk:
    sequence: int | None
    sample_count: int
    rate: int
    timestamp_ms: int | None
    pcm: bytes


@dataclass(frozen=True)
class ParseError:
    reason: str
    raw: str = ""


@dataclass
class ParsedLine:
    """一行的解析结果。

    刻意同时允许 `value` 与 `error` 为空：一行可能是设备启动横幅之类的非协议文本，那既不是
    错误也不是数据。把所有无法识别的东西都算成错误，日志会被启动横幅刷屏；反过来把它们
    当成数据，界面就会显示不存在的东西。
    """

    value: Any = None
    error: ParseError | None = None
    #: 是否是 JSON 行。非 JSON 行通常是 ESP-IDF 的日志，属于正常噪声。
    json_line: bool = False
    problems: tuple[str, ...] = field(default=())


def _as_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # JSON 里 1.0 与 1 都会出现；只接受没有小数部分的浮点，避免把 1.5 当成序号。
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _parse_command(payload: dict[str, Any]) -> ParsedLine:
    problems: list[str] = []
    sequence = _as_int(payload, "sequence")
    timestamp_ms = _as_int(payload, "timestamp_ms")
    speech_gain = _as_float(payload, "speech_gain_db")
    external_voice_gain = _as_float(payload, "external_voice_gain_db")
    # noise_reduction_db 已从固件里删除（本产品只有增益与调音）。这里接受它存在，
    # 只为读得懂旧固件的帧与历史日志；不会把它放进快照端给界面。
    noise_reduction = _as_float(payload, "noise_reduction_db")
    media_percent = _as_int(payload, "media_percent")
    confidence = _as_float(payload, "confidence")
    state = _as_str(payload, "state")
    environment = _as_str(payload, "environment")
    link = _as_str(payload, "link")
    mode = _as_str(payload, "mode") or "LEGACY"
    conversation = _as_str(payload, "conversation") or "NONE"
    execution = _as_str(payload, "execution") or "decision_only"
    context = _as_str(payload, "context")
    context_score = _as_float(payload, "context_score")
    context_us = _as_int(payload, "context_us")
    raw_valid = payload.get("context_valid")
    # 只在字段存在时认它：融合第二轮之前的固件根本不发这四个字段，把它们当成"缺字段"
    # 会把每一帧历史日志都标成异常。
    context_valid: bool | None = raw_valid if isinstance(raw_valid, bool) else None
    # v3：自身语音证据。缺字段一律给 None（旧固件不发），而不是 0.0——
    # "没报"和"报了 0"在这个项目里是两件必须分开的事（同 v2 的 external_voice_gain_db）。
    own_voice_confidence = _as_float(payload, "own_voice_confidence")
    own_voice_correlation = _as_float(payload, "own_voice_correlation")
    own_voice_source = _as_str(payload, "own_voice_source")
    imu_gravity = _as_float(payload, "imu_gravity")
    raw_imu_ok = payload.get("imu_ok")
    imu_ok = raw_imu_ok if isinstance(raw_imu_ok, bool) else None
    raw_imu_motion = payload.get("imu_motion")
    imu_motion = raw_imu_motion if isinstance(raw_imu_motion, bool) else None
    imu_drops = _as_int(payload, "imu_drops")

    missing = [
        name
        for name, value in (
            ("sequence", sequence),
            ("timestamp_ms", timestamp_ms),
            ("speech_gain_db", speech_gain),
            ("media_percent", media_percent),
            ("confidence", confidence),
            ("state", state),
            ("environment", environment),
            ("link", link),
        )
        if value is None
    ]
    if missing:
        return ParsedLine(
            error=ParseError("earbud_command 缺字段: " + ", ".join(missing), json.dumps(payload, ensure_ascii=False)[:200]),
            json_line=True,
        )

    schema = _as_int(payload, "schema")
    if schema is None:
        # 固件当前总会发 schema。一旦它不发了，说明协议在演进，此时按 v1 解读要留痕迹。
        schema = SCHEMA_VERSION
        schema = 1
        problems.append("帧里没有 schema 字段，按 v1 解读")
    elif schema not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(f"schema={schema} 不在支持范围 {SUPPORTED_SCHEMA_VERSIONS}")

    # 校验用**实发**上限（8.0），不是产品策略值（6.5）：固件末尾把值钳到 kDemoSpeechGainDb，
    # 所以 8.0 才是正常帧。按 6.5 校验的话，每一帧人声意图都会被标成"超出硬上限"。
    if speech_gain > DEMO_SPEECH_GAIN_DB + 1e-6:
        problems.append(
            f"人声增益 {speech_gain:+.2f} dB 超过固件实发上限 +{DEMO_SPEECH_GAIN_DB} dB"
            f"（产品策略值 +{MAX_SPEECH_GAIN_DB} dB）"
        )
    if speech_gain < -1e-6:
        problems.append(
            f"人声增益 {speech_gain:+.2f} dB 为负数（固件钳在 0…+{DEMO_SPEECH_GAIN_DB} dB）"
        )
    # noise_reduction_db 是"降噪量的大小"，不是负值偏移。固件的钳位是
    # std::clamp(noise_reduction_db_, 0.0f, kMaxNoiseReductionDb)（main/controller.cpp:307），
    # 合法区间因此是 0…+8；真机实测值就是 4.10 这样的正数。屏幕上的「−8 dB 上限」是自然
    # 语言说法，不是这个字段的符号。按"必须为负"去校验，会把每一帧正常帧都标成异常。
    if noise_reduction is not None:
        # 旧固件的字段。仍然校验：设备报了一个越界的值，不管它属于哪一代固件都要能被发现。
        if noise_reduction < -1e-6:
            problems.append(
                f"降噪量 {noise_reduction:.2f} dB 为负数（旧固件给的是 0…+{MAX_NOISE_REDUCTION_DB} 的大小）"
            )
        if noise_reduction > MAX_NOISE_REDUCTION_DB + 1e-6:
            problems.append(
                f"降噪量 {noise_reduction:.2f} dB 超过硬上限 {MAX_NOISE_REDUCTION_DB} dB"
            )

    tuning = _parse_tuning(payload)
    ambient = _parse_float_array(payload, "ambient_band_dbfs")
    if schema >= 2 and external_voice_gain is None:
        problems.append("v2 帧缺 external_voice_gain_db")
    if schema >= 3 and own_voice_confidence is None:
        problems.append("v3 帧缺 own_voice_confidence")
    if external_voice_gain is not None and not 0.0 <= external_voice_gain <= DEMO_SPEECH_GAIN_DB:
        problems.append(f"现实人声增益 {external_voice_gain:+.2f} dB 越界（合法 0…{DEMO_SPEECH_GAIN_DB}）")
    if not 0 <= media_percent <= 100:
        problems.append(f"媒体音量 {media_percent}% 超出 0–100")
    if not 0.0 <= confidence <= 1.0:
        problems.append(f"置信度 {confidence} 不在 [0,1]")
    if state not in KNOWN_STATES:
        problems.append(f"未知状态 {state!r}")
    if environment not in KNOWN_ENVIRONMENTS:
        problems.append(f"未知环境 {environment!r}")

    # 情境模型那四个字段是一组，一起校验。规则按"设备实际会发什么"写：
    #   * 只发了一部分 → 说不清这一帧到底有没有模型输出；
    #   * `context_valid=false` 时固件报占位类别 `unknown`——占位符不该被当成第 10 个类别，
    #     反过来，valid=false 却报了一个真类别是自相矛盾；
    #   * `context_valid=true` 时类别必须是那 9 个之一，置信度必须在 [0,1]。
    context_fields = (context, context_score, context_us, raw_valid)
    reported = [value is not None for value in context_fields]
    if any(reported) and not all(reported):
        problems.append("情境字段只报了一部分（context/context_score/context_valid/context_us 是一组）")
    elif context_valid is True:
        if context not in CONTEXT_CLASSES:
            problems.append(f"未知情境类别 {context!r}")
        if context_score is None or not 0.0 <= context_score <= 1.0:
            problems.append(f"情境置信度 {context_score} 不在 [0,1]")
        if context_us is not None and context_us < 0:
            problems.append(f"情境推理耗时 {context_us} µs 为负数")
    elif context_valid is False:
        if context is not None and context != CONTEXT_UNKNOWN:
            problems.append(
                f"context_valid=false 却报了类别 {context!r}（占位符应是 {CONTEXT_UNKNOWN!r}）"
            )

    return ParsedLine(
        value=CommandFrame(
            sequence=sequence,
            timestamp_ms=timestamp_ms,
            speech_gain_db=d2(speech_gain),
            noise_reduction_db=None if noise_reduction is None else d2(noise_reduction),
            media_percent=media_percent,
            state=state,
            environment=environment,
            confidence=d2(confidence),
            link=link,
            external_voice_gain_db=d2(external_voice_gain or 0.0),
            mode=mode,
            conversation=conversation,
            execution=execution,
            ambient_band_dbfs=ambient,
            tuning=tuning,
            context=context,
            context_score=None if context_score is None else d2(context_score),
            context_valid=context_valid,
            context_us=context_us,
            own_voice_confidence=None if own_voice_confidence is None else d2(own_voice_confidence),
            own_voice_correlation=(
                None if own_voice_correlation is None else d2(own_voice_correlation)
            ),
            imu_gravity=None if imu_gravity is None else d2(imu_gravity),
            own_voice_source=own_voice_source,
            imu_ok=imu_ok,
            imu_motion=imu_motion,
            imu_drops=imu_drops,
            schema_version=schema,
        ),
        json_line=True,
        problems=tuple(problems),
    )


def _parse_tuning(payload: dict[str, Any]) -> Tuning | None:
    """解析 24 频段调音数组。缺字段返回 None——旧固件本来就不发它。"""
    raw = payload.get("eq_gain_db")
    if not isinstance(raw, list):
        return None
    gains: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        gains.append(d2(float(item)))
    if not gains:
        return None
    lifted = [value for value in gains if value > 0.05]
    return Tuning(
        band_gain_db=tuple(gains),
        lifted_bands=len(lifted),
        peak_gain_db=d2(max(gains)),
    )


def _parse_float_array(payload: dict[str, Any], key: str) -> tuple[float, ...]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return ()
    values: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return ()
        values.append(d2(float(item)))
    return tuple(values)


def _parse_latency(payload: dict[str, Any]) -> ParsedLine:
    frames = _as_int(payload, "frames")
    total_p50 = _as_float(payload, "total_p50_ms")
    total_p95 = _as_float(payload, "total_p95_ms")
    if frames is None or total_p50 is None or total_p95 is None:
        return ParsedLine(
            error=ParseError("latency 缺 frames/total_p50_ms/total_p95_ms", ""), json_line=True
        )

    stages: list[StageStat] = []
    raw_stages = payload.get("stages")
    problems: list[str] = []
    if isinstance(raw_stages, list):
        for item in raw_stages:
            if not isinstance(item, dict):
                problems.append("latency.stages 里有一项不是对象")
                continue
            name = _as_str(item, "stage")
            p50 = _as_float(item, "p50_ms")
            if name is None or p50 is None:
                problems.append("latency.stages 里有一项缺 stage/p50_ms")
                continue
            # 缺字段要留下痕迹，不能静默当 0。原因很直接：n=0 在界面上是有含义的
            # （对端未接通），把"固件少发了一个 n"显示成"对端未接通"就是给出错误解释。
            n_value = _as_int(item, "n")
            p95_value = _as_float(item, "p95_ms")
            max_value = _as_float(item, "max_ms")
            if n_value is None:
                problems.append(f"latency.stages[{name}] 缺 n，按 0 处理（可能是字段缺失）")
            if p95_value is None or max_value is None:
                problems.append(f"latency.stages[{name}] 缺 p95_ms/max_ms，按 0 处理")
            stages.append(
                StageStat(
                    stage=name,
                    samples=n_value or 0,
                    p50_ms=d2(p50),
                    p95_ms=d2(p95_value or 0.0),
                    max_ms=d2(max_value or 0.0),
                    local=name in LOCAL_STAGES,
                )
            )
    else:
        problems.append("latency 缺 stages 数组")

    return ParsedLine(
        value=LatencyReport(
            frames=frames,
            total_p50_ms=d2(total_p50),
            total_p95_ms=d2(total_p95),
            overhead_mean_us=d2(_as_float(payload, "overhead_mean_us") or 0.0),
            overhead_max_us=d2(_as_float(payload, "overhead_max_us") or 0.0),
            overhead_samples=_as_int(payload, "overhead_samples") or 0,
            stages=tuple(stages),
        ),
        json_line=True,
        problems=tuple(problems),
    )


def _parse_capture(payload: dict[str, Any]) -> ParsedLine:
    state = _as_str(payload, "state")
    if state is None:
        return ParsedLine(error=ParseError("capture 缺 state", ""), json_line=True)
    if state not in ("started", "stopped", "finished", "encode_failed", "progress"):
        return ParsedLine(
            value=CaptureEvent(state=state),
            json_line=True,
            problems=(f"未知的录音状态 {state!r}",),
        )
    progress = None
    if state == "progress":
        progress = {
            key: value
            for key, value in payload.items()
            if key not in ("type", "state") and isinstance(value, int) and not isinstance(value, bool)
        }
    return ParsedLine(
        value=CaptureEvent(
            state=state,
            seconds=_as_int(payload, "seconds"),
            rate=_as_int(payload, "rate"),
            chunks=_as_int(payload, "chunks"),
            samples=_as_int(payload, "samples"),
            progress=progress,
        ),
        json_line=True,
    )


def _parse_route(payload: dict[str, Any]) -> ParsedLine:
    state = _as_str(payload, "state")
    if state not in ("active", "inactive", "offline"):
        return ParsedLine(
            error=ParseError(f"route 的 state 无效: {state!r}", ""), json_line=True
        )
    return ParsedLine(
        value=RouteEvent(state=state, device=_as_str(payload, "device")),
        json_line=True,
    )


def _parse_mic_chunk(payload: dict[str, Any]) -> ParsedLine:
    encoded = _as_str(payload, "pcm")
    if encoded is None:
        return ParsedLine(error=ParseError("mic_chunk 缺 pcm", ""), json_line=True)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        return ParsedLine(error=ParseError(f"mic_chunk 的 base64 无法解码: {exc}", ""), json_line=True)
    if len(raw) % 2 != 0:
        return ParsedLine(
            error=ParseError(f"mic_chunk 的 PCM 字节数为奇数（{len(raw)}），不是 16 位样本", ""),
            json_line=True,
        )

    problems: list[str] = []
    rate = _as_int(payload, "rate")
    if rate is None:
        rate = CAPTURE_RATE_HZ
        problems.append(f"mic_chunk 未报采样率，按 {CAPTURE_RATE_HZ} Hz 处理")
    sample_count = _as_int(payload, "samples")
    if sample_count is None:
        sample_count = len(raw) // 2
        problems.append("mic_chunk 未报样本数，按 pcm 长度推断")
    elif sample_count != len(raw) // 2:
        problems.append(
            f"mic_chunk 自称 {sample_count} 点，实际 {len(raw) // 2} 点（以实际长度为准）"
        )

    return ParsedLine(
        value=MicChunk(
            sequence=_as_int(payload, "sequence"),
            sample_count=len(raw) // 2,
            rate=rate,
            timestamp_ms=_as_int(payload, "timestamp_ms"),
            pcm=raw,
        ),
        json_line=True,
        problems=tuple(problems),
    )


def parse_line(line: str) -> ParsedLine:
    """解析串口的一行。

    返回 ParsedLine。非 JSON 行、未知 type、损坏 JSON 都各自有明确出口——上位机需要能
    区分"设备在说别的事"和"设备的协议输出坏了"。
    """
    text = line.strip()
    if not text:
        return ParsedLine()
    if not text.startswith("{"):
        # ESP-IDF 的日志横幅、互斥锁报错等。它们是正常噪声，不是协议数据。
        return ParsedLine()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParsedLine(
            error=ParseError(f"JSON 解析失败: {exc.msg}", text[:200]), json_line=True
        )
    if not isinstance(payload, dict):
        return ParsedLine(error=ParseError("JSON 顶层不是对象", text[:200]), json_line=True)

    kind = payload.get("type")
    if kind == "earbud_command":
        return _parse_command(payload)
    if kind == "latency":
        return _parse_latency(payload)
    if kind == "capture":
        return _parse_capture(payload)
    if kind == "route":
        return _parse_route(payload)
    if kind == "mic_chunk":
        return _parse_mic_chunk(payload)
    return ParsedLine(
        error=ParseError(f"未知的 type={kind!r}", text[:200]), json_line=True
    )


def iter_json_objects(chunks: Iterable[bytes]) -> Iterable[str]:
    """把任意切分的字节流切成完整的行。

    串口读到的数据不保证按行对齐：一次 read 可能返回半行，也可能一次返回几十行。
    这里只负责把字节拼成行，不做解析——把它与 parse_line 分开，是为了让"切行错了"
    和"解析错了"是两类可区分的故障。
    """
    pending = b""
    for chunk in chunks:
        pending += chunk
        while True:
            index = pending.find(b"\n")
            if index < 0:
                break
            line, pending = pending[:index], pending[index + 1 :]
            yield line.decode("utf-8", errors="replace")
    if pending:
        yield pending.decode("utf-8", errors="replace")
