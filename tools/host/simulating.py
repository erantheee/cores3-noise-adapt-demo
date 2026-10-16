"""模拟源：按固件的行为生成帧。

它**不是**用来假装有设备的。它解决两个具体问题：

1. 没有设备时，上位机自己的链路（切行 → 解析 → 状态归约 → 界面 → 记录）仍然可以
   端到端跑通并被测出来。否则"上位机坏了"和"设备没插"在现场分不开。
2. 界面上的显示逻辑（阈值条、超限告警、丢帧提示、录音态）需要可复现的输入才能自测。

生成的行为刻意贴住固件的真实数字，而不是随便造几个数：

    控制节拍        100 ms（main/app_main.cpp: kControlPeriodMs）
    协议            schema 2：mode / conversation / external_voice_gain_db / 24 段 eq_gain_db。
                    **不发 noise_reduction_db**——那个字段已从固件删除（main/controller.hpp）
    状态机          从安静 → 噪声 → 人声 → 高噪声，中间有斜坡而不是跳变，
                    因为固件本身是"建议值逐步逼近目标"（controller.cpp）
    人声增益上限    +8.0 dB（kDemoSpeechGainDb，固件实际下发的上限），模拟值永不越界；
                    song_eq 的逐段建议另有 4 dB 上限，safety_hold 禁止正增益
    latency 汇总    每 10 s 一次，字段与 emit_latency_report 一致，含 n=0 的未实现阶段

`plant_fault` 与 `drop_frames` 是给测试用的注入点：前者复现"麦克风异常"，
后者复现 USB 丢帧——两者都必须能被上位机如实报出来。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from .protocol import CAPTURE_RATE_HZ, DEMO_SPEECH_GAIN_DB, SCHEMA_VERSION

#: 控制节拍，与固件 main/app_main.cpp 的 kControlPeriodMs 一致（100 ms）。
CONTROL_PERIOD_S = 0.1

#: 与环境对应的决策，按固件 controller.cpp 的四条分支给（不是随便造几个数）：
#:   QUIET → flat，什么都不做；NOISY → song_eq，只给逐段建议、标量增益为 0；
#:   VOICE → external_voice_intent，语音段抬到实发上限、媒体让位；
#:   LOUD  → safety_hold，禁止正增益（这一条最容易被模拟源写错成"越大声抬越多"）。
TARGETS = {
    "QUIET": {"gain": 0.0, "media": 100, "confidence": 0.95, "mode": "FLAT",
              "conversation": "NONE"},
    "NOISY": {"gain": 0.0, "media": 100, "confidence": 0.98, "mode": "SONG_EQ",
              "conversation": "NONE"},
    "VOICE": {"gain": 7.6, "media": 30, "confidence": 0.99, "mode": "EXTERNAL_VOICE_INTENT",
              "conversation": "LIKELY"},
    "LOUD": {"gain": 0.0, "media": 100, "confidence": 1.0, "mode": "SAFETY_HOLD",
             "conversation": "NONE"},
}

#: 模拟的情境模型输出：固件那 4 个环境各自对应的类别与分数。**这是模拟值，不是测量值**——
#: 它的用途只是让面板在 `--mock` 下把情境卡跑起来（真机现在的实际输出见 plan-status 第 29 项：
#: 安静房间里模型几乎总是报 other）。
CONTEXT_FOR_ENVIRONMENT = {
    "QUIET": ("quiet", 0.93),
    "NOISY": ("steady", 0.88),
    "VOICE": ("near_voice", 0.72),
    "LOUD": ("street", 0.61),
}

#: 固件要攒满 2 秒窗（20 帧 × 100 ms）才给判断，在那之前报 valid=false + 占位类别 unknown。
#: 模拟源必须复刻这一段，否则面板会在那两秒里显示一个凭空的类别。
CONTEXT_WARMUP_S = 2.0

#: 环境序列与各段时长（秒）。刻意包含升段与降段，让面板上的趋势线有形状。
SCHEDULE: tuple[tuple[str, float], ...] = (
    ("QUIET", 5.0),
    ("NOISY", 6.0),
    ("VOICE", 5.0),
    ("LOUD", 5.0),
    ("VOICE", 3.0),
    ("NOISY", 4.0),
    ("QUIET", 6.0),
)


@dataclass
class MockDevice:
    """按 100 ms 节拍（与固件 kControlPeriodMs 一致）产出一行行串口文本。"""

    seed: int = 7
    speed: float = 1.0
    #: 每隔多少秒发一次 latency 汇总（与固件一致：10 s）。
    latency_period_s: float = 10.0
    #: 强制为某一环境；None 表示按 SCHEDULE 循环。
    fixed_environment: str | None = None
    #: True 时持续报 MIC FAULT，用于验证界面与告警。
    plant_fault: bool = False
    #: 要跳过的序号集合，用来复现 USB 丢帧。
    drop_frames: set[int] = field(default_factory=set)
    #: 序号跳变处的抖动概率（0 表示不丢）。
    drop_probability: float = 0.0
    #: 是否产出录音块（用于验证记录链路）。
    recording: bool = False
    random_seed: int = 11

    _sequence: int = field(default=0, init=False)
    _next_emit_s: float = field(default=0.0, init=False)
    _gain: float = field(default=0.0, init=False)
    _media: float = field(default=100.0, init=False)
    _chunk_sequence: int = field(default=0, init=False)
    _samples_since_chunk: int = field(default=0, init=False)
    _random: random.Random = field(init=False)
    _next_latency_s: float = field(default=0.0, init=False)
    _last_environment: str = field(default="QUIET", init=False)

    def __post_init__(self) -> None:
        self._random = random.Random(self.random_seed)

    # ------------------------------------------------------------------ 环境

    def environment_at(self, elapsed_s: float) -> str:
        if self.fixed_environment:
            return self.fixed_environment
        total = sum(length for _, length in SCHEDULE)
        position = elapsed_s % total
        for name, length in SCHEDULE:
            if position < length:
                return name
            position -= length
        return SCHEDULE[-1][0]

    def _ramp(self, current: float, target: float, dt_s: float, tau_s: float = 0.8) -> float:
        # 一阶逼近，而不是直接跳变：固件给的是"建议值逐渐逼近目标"，
        # 面板上的趋势线必须能看出这个形状，否则自测就等于在测一条直线。
        alpha = 1.0 - pow(2.718281828, -dt_s / tau_s)
        return current + (target - current) * alpha

    # ------------------------------------------------------------------ 产出

    def advance(self, elapsed_s: float) -> list[str]:
        """推进到 elapsed_s，返回这一拍要吐出的所有行。"""
        lines: list[str] = []
        dt = CONTROL_PERIOD_S
        while self._next_emit_s <= elapsed_s:
            lines.extend(self._emit(self._next_emit_s, dt))
            self._next_emit_s += dt
        return lines

    def _emit(self, at_s: float, dt: float) -> list[str]:
        lines: list[str] = []
        environment = self.environment_at(at_s)
        target = TARGETS[environment]
        self._gain = self._ramp(self._gain, min(target["gain"], DEMO_SPEECH_GAIN_DB), dt)
        self._media = self._ramp(self._media, float(target["media"]), dt, tau_s=1.6)

        # 情境模型：前 2 秒窗没满，固件报 valid=false；之后按环境给一个类别。一次前向的
        # 耗时取真机实测的量级（p50 151–158 µs，见 plan-status 第 29 项）。
        context_valid = at_s >= CONTEXT_WARMUP_S
        context_class, context_score = CONTEXT_FOR_ENVIRONMENT[environment]
        context_us = int(155 + 25 * self._random.random())

        # 24 段建议的形状按模式给，与固件一致：人声模式抬 8–16 段（两端 0.75 收口），
        # song_eq 抬全部频段但夹在 4 dB 内，flat / safety_hold 一律 0 dB。
        mode = str(target["mode"])
        bands = [0.0] * 24
        if mode == "EXTERNAL_VOICE_INTENT":
            for band in range(8, 17):
                bands[band] = round(self._gain * (0.75 if band in (8, 16) else 1.0), 2)
        elif mode == "SONG_EQ":
            for band in range(24):
                importance = 1.0 if 8 <= band <= 16 else (0.45 if band < 5 else 0.70)
                bands[band] = round(min(4.0, 4.0 * 0.6 * importance), 2)

        self._sequence += 1
        if self.drop_probability and self._random.random() < self.drop_probability:
            self._sequence += 1  # 制造一次序号跳变，模拟 USB 丢帧
        if self._sequence in self.drop_frames:
            pass  # 序号照常前进，但这一帧不输出
        else:
            state = "MIC FAULT" if self.plant_fault else self._state_for(environment)
            # 抖动只取正值：人声增益与降噪量在固件里都是 clamp(_, 0, max)，
            # 负抖动会造出固件不可能给出的值，自测就会去测一个不存在的状态。
            jitter = self._random.uniform(0.0, 0.08)
            lines.append(
                json.dumps(
                    {
                        "type": "earbud_command",
                        "schema": SCHEMA_VERSION,
                        "sequence": self._sequence,
                        "timestamp_ms": int(at_s * 1000),
                        "speech_gain_db": round(max(0.0, self._gain + jitter), 2),
                        "external_voice_gain_db": round(max(0.0, self._gain + jitter), 2),
                        "media_percent": int(round(self._media)),
                        "state": state,
                        "environment": environment,
                        "mode": mode,
                        "conversation": target["conversation"],
                        "confidence": round(min(1.0, target["confidence"] - abs(jitter)), 2),
                        "execution": "decision_only",
                        "link": "simulated",
                        "eq_gain_db": bands,
                        # 情境模型（融合第二轮）。字段名、占位符、以及"窗没满就报 false"的
                        # 行为都照 main/app_main.cpp 的实发内容来，这样面板的三种状态
                        # （没有字段 / 还没判断 / 有判断）在 --mock 下都能被看到。
                        "context": context_class if context_valid else "unknown",
                        "context_score": round(context_score, 3) if context_valid else 0.0,
                        "context_valid": context_valid,
                        "context_us": context_us,
                        # 24 段环境谱：面板用它画趋势与"设备实测"的出处。
                        "ambient_band_dbfs": [round(-72.0 + 40.0 * (i / 23.0), 1)
                                              for i in range(24)],
                    },
                    ensure_ascii=False,
                )
            )

        if self.recording:
            lines.extend(self._emit_chunks(dt))

        if at_s >= self._next_latency_s:
            self._next_latency_s = at_s + self.latency_period_s
            lines.append(self._latency_line())
        return lines

    def _state_for(self, environment: str) -> str:
        if environment == self._last_environment:
            return {
                "QUIET": "HOLDING",
                "NOISY": "ADJUSTING",
                "VOICE": "ADJUSTING",
                "LOUD": "ADJUSTING",
            }[environment]
        self._last_environment = environment
        return {
            "QUIET": "RECOVERING",
            "NOISY": "OBSERVING",
            "VOICE": "ADJUSTING",
            "LOUD": "ADJUSTING",
        }[environment]

    def _emit_chunks(self, dt: float) -> list[str]:
        # 设备每个 1024 样本块发一行（16 kHz 下 64 ms）。
        self._samples_since_chunk += int(CAPTURE_RATE_HZ * dt)
        lines: list[str] = []
        while self._samples_since_chunk >= 1024:
            self._samples_since_chunk -= 1024
            self._chunk_sequence += 1
            # 一段低电平的确定性信号：够验证"解出来的样本数与时长对不对"，
            # 不假装它是真实音频。
            pcm = bytes(1024 * 2)
            import base64

            lines.append(
                json.dumps(
                    {
                        "type": "mic_chunk",
                        "sequence": self._chunk_sequence,
                        "timestamp_ms": int(self._next_emit_s * 1000),
                        "samples": 1024,
                        "rate": CAPTURE_RATE_HZ,
                        "pcm": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )
        return lines

    def _latency_line(self) -> str:
        # 字段与 main/app_main.cpp 的 emit_latency_report 一致，包括 n=0 的未实现阶段——
        # 上位机必须能显示"声纹推理 0.000 ms（未实现）"，而不是把 0 当成"很快"。
        stages = [
            ("capture", 100.03, 100.03, 100.05, 68),
            ("analyse", 7.02, 7.03, 7.03, 68),
            ("embed", 0.0, 0.0, 0.0, 68),
            ("decide", 0.06, 0.06, 0.06, 68),
            ("transport", 0.0, 0.0, 0.0, 0),
            ("apply", 0.0, 0.0, 0.0, 0),
            ("render", 0.0, 0.0, 0.0, 0),
        ]
        return json.dumps(
            {
                "type": "latency",
                "frames": 68,
                "total_p50_ms": 157.13,
                "total_p95_ms": 157.13,
                "overhead_mean_us": 1.45,
                "overhead_max_us": 2.0,
                "overhead_samples": 68,
                "stages": [
                    {"stage": name, "n": n, "p50_ms": p50, "p95_ms": p95, "max_ms": mx}
                    for name, p50, p95, mx, n in stages
                ],
            }
        )
