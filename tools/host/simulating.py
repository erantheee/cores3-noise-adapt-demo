"""模拟源：按固件的行为生成帧。

它**不是**用来假装有设备的。它解决两个具体问题：

1. 没有设备时，上位机自己的链路（切行 → 解析 → 状态归约 → 界面 → 记录）仍然可以
   端到端跑通并被测出来。否则"上位机坏了"和"设备没插"在现场分不开。
2. 界面上的显示逻辑（阈值条、超限告警、丢帧提示、录音态）需要可复现的输入才能自测。

生成的行为刻意贴住固件的真实数字，而不是随便造几个数：

    控制节拍        150 ms（main/app_main.cpp: kControlPeriodMs）
    状态机          从安静 → 噪声 → 人声 → 高噪声，中间有斜坡而不是跳变，
                    因为固件本身是"建议值逐步逼近目标"（controller.cpp）
    人声增益上限    +6.5 dB（kMaxSpeechGainDb），模拟值永不越界
    降噪上限        −8 dB（kMaxNoiseReductionDb）
    latency 汇总    每 10 s 一次，字段与 emit_latency_report 一致，含 n=0 的未实现阶段

`plant_fault` 与 `drop_frames` 是给测试用的注入点：前者复现"麦克风异常"，
后者复现 USB 丢帧——两者都必须能被上位机如实报出来。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from .protocol import CAPTURE_RATE_HZ, MAX_NOISE_REDUCTION_DB, MAX_SPEECH_GAIN_DB

#: 与环境对应的建议值。数字取自固件的硬上限与设备屏幕上的量级。
#: `noise` 是**降噪量的大小**（0…+8 dB），与固件 std::clamp(_, 0, kMaxNoiseReductionDb)
#: 的语义一致——不是负值偏移。这一点在模拟源里写错过一次：按负数生成的帧会被上位机的
#: 校验全部判为异常，而那是模拟源的错，不是校验的错。
TARGETS = {
    "QUIET": {"gain": 0.0, "noise": 0.0, "media": 100, "confidence": 0.95},
    "NOISY": {"gain": 2.6, "noise": 4.0, "media": 94, "confidence": 0.98},
    "VOICE": {"gain": 4.2, "noise": 3.2, "media": 92, "confidence": 0.99},
    "LOUD": {"gain": 5.6, "noise": 7.4, "media": 85, "confidence": 1.0},
}

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
    """按 150 ms 节拍产出一行行串口文本。"""

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
    _noise: float = field(default=0.0, init=False)
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
        dt = 0.10
        while self._next_emit_s <= elapsed_s:
            lines.extend(self._emit(self._next_emit_s, dt))
            self._next_emit_s += dt
        return lines

    def _emit(self, at_s: float, dt: float) -> list[str]:
        lines: list[str] = []
        environment = self.environment_at(at_s)
        target = TARGETS[environment]
        self._gain = self._ramp(self._gain, min(target["gain"], MAX_SPEECH_GAIN_DB), dt)
        self._noise = self._ramp(self._noise, min(target["noise"], MAX_NOISE_REDUCTION_DB), dt)
        self._media = self._ramp(self._media, float(target["media"]), dt, tau_s=1.6)

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
                        "schema": 1,
                        "sequence": self._sequence,
                        "timestamp_ms": int(at_s * 1000),
                        "speech_gain_db": round(max(0.0, self._gain + jitter), 2),
                        "noise_reduction_db": round(max(0.0, self._noise + jitter / 3), 2),
                        "media_percent": int(round(self._media)),
                        "state": state,
                        "environment": environment,
                        "confidence": round(min(1.0, target["confidence"] - abs(jitter)), 2),
                        "link": "simulated",
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
            ("capture", 150.045, 150.048, 150.058, 68),
            ("analyse", 7.033, 7.036, 7.038, 68),
            ("embed", 0.0, 0.0, 0.0, 68),
            ("decide", 0.051, 0.051, 0.060, 68),
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
