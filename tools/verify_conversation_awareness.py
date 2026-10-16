#!/usr/bin/env python3
"""真机验证：Apple 式对话感知在 CoreS3 上到底成不成立。

为什么单独一个脚本
------------------
`docs/apple-conversation-awareness.md` 第 11.6 节列了四项只有真机能回答的问题。它们的共同点是
**光看 media_percent 分不出来**：

    * "证据没来"（IMU 没初始化 / 骨传导判据不成立）→ 要改证据；
    * "证据来了但没下压"（状态机 / 时序参数）→ 要改参数；
    * "不该下压的时候下压了"（运动、旁人说话）→ 要改判据的门限或运动否决。

所以这个脚本按**说话人分相**跑，逐相看 own_voice 证据、下压行为与 IMU 健康度，并把三项分开断言。

用法
----
    # 真机（先让出串口：sh tools/free_port.sh --kill）
    ./.venv/bin/python tools/verify_conversation_awareness.py --port /dev/cu.usbmodem1101

    # 只看串口在不在、固件版本对不对，不跑相
    ./.venv/bin/python tools/verify_conversation_awareness.py --port ... --dry-run

    # 离线分析已抓下来的日志（只需要每行 JSON）
    ./.venv/bin/python tools/verify_conversation_awareness.py --file capture.log

退出码：0 全过 · 1 有断言失败 · 2 设备/固件不对 · 3 串口被占
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from host.protocol import parse_line  # noqa: E402

BAUD = 921600
TICK_MS = 100.0
#: 状态机规格：确认 200 ms + 3τ(duck 150 ms) = 650 ms 之内应当压到 60% 以下。
DUCK_LATENCY_BUDGET_MS = 650.0
#: 判定"自己说话"的证据门限。低于它说明骨传导判据没成立，不是状态机的问题。
EVIDENCE_FLOOR = 0.30
#: 重力基线（±4g，m/s²）。静止时偏离这个区间说明量程/单位/装配有问题。
GRAVITY_RANGE = (9.0, 10.5)


def say(text: str) -> None:
    if not shutil.which("say"):
        return
    if subprocess.run(["say", "-v", "Tingting", text], capture_output=True).returncode != 0:
        subprocess.run(["say", text], check=False)


@dataclass
class Frame:
    t_ms: float
    schema: int
    media_percent: int
    conversation: str
    confidence: float
    own_voice_confidence: float | None
    own_voice_correlation: float | None
    own_voice_source: str | None
    imu_ok: bool | None
    imu_motion: bool | None
    imu_gravity: float | None
    imu_drops: int | None
    imu_samples: int | None = None
    evidence_policy: str | None = None
    problems: tuple[str, ...] = ()


@dataclass
class Phase:
    name: str
    prompt: str
    seconds: float
    frames: list[Frame] = field(default_factory=list)

    # ---- 逐相统计 ----
    def _values(self, getter) -> list[float]:
        return [v for v in (getter(f) for f in self.frames) if v is not None]

    @property
    def own_voice_peak(self) -> float:
        values = self._values(lambda f: f.own_voice_confidence)
        return max(values) if values else 0.0

    @property
    def own_voice_p50(self) -> float:
        values = self._values(lambda f: f.own_voice_confidence)
        return statistics.median(values) if values else 0.0

    @property
    def correlation_p50(self) -> float:
        values = self._values(lambda f: f.own_voice_correlation)
        return statistics.median(values) if values else 0.0

    @property
    def media_min(self) -> int:
        return min((f.media_percent for f in self.frames), default=100)

    @property
    def ducked_frames(self) -> int:
        return sum(1 for f in self.frames if f.media_percent < 99)

    @property
    def motion_frames(self) -> int:
        return sum(1 for f in self.frames if f.imu_motion)

    @property
    def gravity_p50(self) -> float | None:
        values = self._values(lambda f: f.imu_gravity)
        return statistics.median(values) if values else None

    @property
    def sample_rate_hz(self) -> float | None:
        """这一相的 IMU 采样率。控制拍 100 ms、IMU 100 Hz，所以每相应当读到约 100 Hz。"""
        first = next((f for f in self.frames if f.imu_samples is not None), None)
        last = next((f for f in reversed(self.frames) if f.imu_samples is not None), None)
        if first is None or last is None or last.t_ms <= first.t_ms:
            return None
        return (last.imu_samples - first.imu_samples) / ((last.t_ms - first.t_ms) / 1000.0)

    @property
    def drops_last(self) -> int | None:
        return self.frames[-1].imu_drops if self.frames else None

    @property
    def first_evidence_ms(self) -> float | None:
        for f in self.frames:
            if (f.own_voice_confidence or 0.0) >= 0.5:
                return f.t_ms
        return None

    @property
    def first_duck_ms(self) -> float | None:
        for f in self.frames:
            if f.media_percent < 60:
                return f.t_ms
        return None

    def summary(self) -> dict:
        return {
            "phase": self.name,
            "frames": len(self.frames),
            "own_voice_peak": round(self.own_voice_peak, 3),
            "own_voice_p50": round(self.own_voice_p50, 3),
            "correlation_p50": round(self.correlation_p50, 3),
            "media_min": self.media_min,
            "ducked_frames": self.ducked_frames,
            "motion_frames": self.motion_frames,
            "gravity_p50": None if self.gravity_p50 is None else round(self.gravity_p50, 3),
            "imu_drops": self.drops_last,
            "imu_hz": None if self.sample_rate_hz is None else round(self.sample_rate_hz, 1),
            "imu_samples_dropped": (
                None
                if not self.frames or self.frames[0].imu_drops is None
                else self.frames[-1].imu_drops - self.frames[0].imu_drops
            ),
            "duck_latency_ms": self.duck_latency_ms(),
        }

    def duck_latency_ms(self) -> float | None:
        evidence = self.first_evidence_ms
        duck = self.first_duck_ms
        if evidence is None or duck is None:
            return None
        return round(duck - evidence, 1)


PHASES: list[tuple[str, str, float]] = [
    ("baseline", "保持安静，不要动设备，十秒。", 10.0),
    ("self_speech", "现在开始说话，连着说十二秒，就像跟人聊天。", 12.0),
    ("quiet_after", "停下来，保持安静十秒。", 10.0),
    ("motion", "把设备拿起来在手里走动，或者轻轻晃动它，十秒。", 10.0),
    ("other_speech", "请另一个人对着设备说话，或者用手机在设备旁边放一段播客，十二秒。", 12.0),
]


def read_frames(port: str, phase: Phase, start_ms: float) -> None:
    import serial  # noqa: PLC0415

    connection = serial.Serial(port=port, baudrate=BAUD, timeout=0.2)
    # ESP32-S3 内置 USB Serial/JTAG 在 DTR 未置位时根本不发数据（tools/record_from_device.py 踩过）。
    connection.dtr = True
    connection.rts = True
    try:
        deadline = time.monotonic() + phase.seconds
        while time.monotonic() < deadline:
            raw = connection.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            if "earbud_command" not in line:
                continue
            parsed = parse_line(line)
            if parsed.value is None or not hasattr(parsed.value, "media_percent"):
                continue
            f = parsed.value
            phase.frames.append(
                Frame(
                    t_ms=(time.monotonic() * 1000.0 - start_ms),
                    schema=f.schema_version,
                    media_percent=f.media_percent,
                    conversation=f.conversation,
                    confidence=f.confidence,
                    own_voice_confidence=f.own_voice_confidence,
                    own_voice_correlation=f.own_voice_correlation,
                    own_voice_source=f.own_voice_source,
                    imu_ok=f.imu_ok,
                    imu_motion=f.imu_motion,
                    imu_gravity=f.imu_gravity,
                    imu_drops=f.imu_drops,
                    imu_samples=f.imu_samples,
                    evidence_policy=f.evidence_policy,
                    problems=parsed.problems,
                )
            )
    finally:
        connection.close()


def analyse(phases: list[Phase]) -> tuple[list[dict], list[str]]:
    checks: list[dict] = []
    failures: list[str] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    by_name = {p.name: p for p in phases}
    # 离线模式：日志里没有"相"的边界，分相断言不可能成立。报统计 + 版本检查，
    # 其余显式说明跳过——让脚本在离线时"绿"起来，比让它报一堆假的失败更危险。
    offline = "baseline" not in by_name
    if offline:
        schemas = {f.schema for p in phases for f in p.frames}
        v3 = any((f.own_voice_confidence is not None) for p in phases for f in p.frames)
        record("C1 固件是 v3（带自身语音遥测）", v3,
               f"帧里的 schema={sorted(schemas)}，own_voice 字段{'有' if v3 else '没有'}")
        record("C0 离线模式", True,
               f"日志 {sum(len(p.frames) for p in phases)} 帧：只报统计，分相断言（C2/C3/C5/C6/C7/C8/C9）已跳过")
        return checks, failures
    baseline = by_name.get("baseline")
    self_speech = by_name.get("self_speech")
    quiet_after = by_name.get("quiet_after")
    motion = by_name.get("motion")
    other_speech = by_name.get("other_speech")

    # C1 固件版本：没有 v3 字段就说明烧的不是这一版，后面所有断言都没有意义。
    schemas = {f.schema for p in phases for f in p.frames}
    v3 = any((f.own_voice_confidence is not None) for p in phases for f in p.frames)
    record("C1 固件是 v3（带自身语音遥测）", v3,
           f"帧里的 schema={sorted(schemas)}，own_voice 字段{'有' if v3 else '没有'}")

    # C2 IMU 就绪
    if baseline is not None and baseline.frames:
        ok_frames = sum(1 for f in baseline.frames if f.imu_ok)
        record("C2 IMU 就绪", ok_frames >= 0.9 * len(baseline.frames),
               f"静止相 {ok_frames}/{len(baseline.frames)} 帧 imu_ok=true")
    else:
        record("C2 IMU 就绪", False, "静止相没有收到任何帧")

    # C3 量程/单位：重力基线
    if baseline is not None and baseline.gravity_p50 is not None:
        gravity = baseline.gravity_p50
        record("C3 重力基线（±4g，m/s²）", GRAVITY_RANGE[0] <= gravity <= GRAVITY_RANGE[1],
               f"静止相 imu_gravity p50={gravity:.2f}，期望 {GRAVITY_RANGE[0]}–{GRAVITY_RANGE[1]}")
    else:
        record("C3 重力基线（±4g，m/s²）", False, "没有 imu_gravity 字段")

    # C4 帧率：控制拍 100 ms，帧数不该明显少于预期
    for phase in phases:
        if not phase.frames or phase.seconds <= 0:
            continue
        expected = phase.seconds * 1000.0 / TICK_MS
        ratio = len(phase.frames) / expected if expected else 0.0
        record(f"C4 {phase.name} 帧率", ratio >= 0.8,
               f"{len(phase.frames)} 帧 / 预期 {expected:.0f}（{ratio * 100:.0f}%）")

    # C4c IMU 采样率："跑在 100 Hz"必须是量出来的。允许 ±10%：给控制任务的抖动留余量，
    # 但半速（队列/总线被拖住）会立刻现形。
    for phase in phases:
        rate = phase.sample_rate_hz
        if rate is None or not phase.frames:
            continue
        record(f"C4c {phase.name} IMU 采样率", 90.0 <= rate <= 110.0,
               f"{rate:.1f} Hz（期望 100 Hz）")

    # C4b IMU 丢采样：控制任务与分析/推理同跑时最容易出问题的地方。
    # 看的是**整个过程里的增量**，不是某一帧的绝对值。
    dropped = None
    for phase in phases:
        if phase.frames and phase.frames[0].imu_drops is not None and phase.frames[-1].imu_drops is not None:
            dropped = phase.frames[-1].imu_drops - phase.frames[0].imu_drops
    if dropped is not None:
        record("C4b IMU 丢采样", dropped == 0, f"验证过程 imu_drops 增量 {dropped}")
    else:
        record("C4b IMU 丢采样", False, "帧里没有 imu_drops 字段")

    # C5 骨传导证据成立：自己说话相必须显著高于静止相
    evidence_ok = False
    if self_speech is not None and baseline is not None:
        peak = self_speech.own_voice_peak
        base = baseline.own_voice_peak
        evidence_ok = peak >= EVIDENCE_FLOOR and peak >= base + 0.15
        record("C5 自身语音证据成立", evidence_ok,
               f"说话相 own_voice 峰值 {peak:.2f} vs 静止相 {base:.2f}（门限 {EVIDENCE_FLOOR}）")
        # C5b：来源必须与**帧里自报的策略**一致。用策略而不是硬编码 "bone"，
        # 是因为演示模式（near_field_demo）是刻意的选择，而把演示说成骨传导才是错误。
        expected_by_policy = {"bone_only": {"bone"}, "near_field_demo": {"near_field"},
                              "disabled": {"none"}}
        policy = next((f.evidence_policy for p in phases for f in p.frames if f.evidence_policy), None)
        sources = {f.own_voice_source for f in self_speech.frames if f.own_voice_source}
        if policy in expected_by_policy:
            want = expected_by_policy[policy]
            record(f"C5b 证据来源与策略一致（{policy}）", sources == want,
                   f"说话相 source={sorted(sources)}，期望 {sorted(want)}")
        else:
            record("C5b 证据来源与策略一致", False, f"帧里没有 own_voice_policy（source={sorted(sources)}）")

    # C6 触发时序：证据 ≥0.5 到下压 <60% 的间隔
    if self_speech is not None:
        latency = self_speech.duck_latency_ms()
        if latency is None:
            # 提示必须跟着 C5 的结论走。第一版对两种情况用同一句话，于是"证据明明到了"
            # 那次也提示"先看证据"——而把这两种情况搞混正是这个脚本存在的理由。
            hint = ("证据到了但没下压 → 该看状态机与时序（conversation::Params）"
                    if evidence_ok else
                    "证据没来 → 先修证据（骨传导判据 / IMU），别急着调时序参数")
            record("C6 下压时序", False, f"说话相没有观察到下压。{hint}")
        else:
            record("C6 下压时序", latency <= DUCK_LATENCY_BUDGET_MS,
                   f"证据→下压 {latency:.0f} ms（预算 {DUCK_LATENCY_BUDGET_MS:.0f} ms）")

    # C7 恢复
    if self_speech is not None and quiet_after is not None and self_speech.ducked_frames > 0:
        record("C7 对话结束后恢复", quiet_after.media_min > 99,
               f"停止说话相的最低 media_percent={quiet_after.media_min}%")
    else:
        record("C7 对话结束后恢复", False, "没有说话相的下压可供恢复验证（见 C6）")

    # C8 运动否决
    if motion is not None:
        record("C8 运动否决", motion.motion_frames > 0 and motion.media_min > 99,
               f"运动相 motion 帧 {motion.motion_frames}/{len(motion.frames)}，最低 media={motion.media_min}%")

    # C9 旁人说话 / 外放不该触发。演示模式（near_field_demo）下这条**预期会失败**：
    # 代理只知道"有人说话"，这正是它的已知代价（docs/apple-conversation-awareness.md 第 15 节）。
    # 失败信息里要写清这一点，否则看报告的人会以为是新故障。
    if other_speech is not None and other_speech.frames:
        detail = (f"旁人相下压帧 {other_speech.ducked_frames}/{len(other_speech.frames)}，"
                  f"own_voice 峰值 {other_speech.own_voice_peak:.2f}")
        if policy == "near_field_demo" and other_speech.ducked_frames > 0:
            detail += "（演示模式已知代价：代理分不出自己与旁人）"
        record("C9 旁人说话不触发", other_speech.ducked_frames == 0, detail)

    return checks, failures


def make_phase(name: str, seconds: float, *, own_voice: float = 0.02, media: int = 100,
               duck_at_ms: float | None = None, duck_percent: int = 25, motion: bool = False,
               imu_ok: bool = True, gravity: float = 9.81, imu_hz: float = 100.0,
               policy: str = "bone_only", source: str | None = None) -> Phase:
    """造一个相的合成帧序列。只用于 `--selftest`：验证**检查器本身**在已知好坏的数据上
    会不会给出正确结论。真机数据永远从串口来。"""
    frames = []
    count = int(round(seconds * 1000.0 / TICK_MS))
    for index in range(count):
        t = index * TICK_MS
        ducked = duck_at_ms is not None and t >= duck_at_ms
        frames.append(Frame(
            t_ms=t, schema=3, media_percent=(duck_percent if ducked else media),
            conversation="LIKELY" if ducked else "NONE", confidence=0.8,
            own_voice_confidence=own_voice,
            own_voice_correlation=0.9 if own_voice > 0.3 else 0.02,
            own_voice_source=source if source is not None else ("bone" if imu_ok else "none"),
            evidence_policy=policy,
            imu_ok=imu_ok, imu_motion=motion, imu_gravity=gravity, imu_drops=0,
            imu_samples=int(t / 1000.0 * imu_hz)))
    return Phase(name=name, prompt="", seconds=seconds, frames=frames)


def selftest() -> int:
    """用合成数据证明"检查器"能分辨三种失败——这是脚本存在的全部理由。

    它必须能分开：证据没来（C5 挂）、证据来了但没下压（C5 过、C6 挂）、
    以及不该下压时下压了（C9 挂）。做不到这一点，真机那次跑出来的报告就没法用来决策。
    """
    scenarios = [
        ("good", dict(self_speech=dict(own_voice=0.85, duck_at_ms=300.0),
                      quiet_after=dict(media=100)), []),
        ("no_evidence", dict(self_speech=dict(own_voice=0.02)), ["C5", "C6", "C7"]),
        ("evidence_no_duck", dict(self_speech=dict(own_voice=0.85)), ["C6", "C7"]),
        ("ducks_on_others", dict(self_speech=dict(own_voice=0.85, duck_at_ms=300.0),
                                 other_speech=dict(own_voice=0.05, media=25)), ["C9"]),
        # IMU 挂掉 → 控制器 fail-closed：置信度 0、来源 none、不下压。合成数据必须按这个来，
        # 否则就是在拿一个现实中不存在的组合去测检查器。
        ("imu_dead", dict(imu_ok=False, gravity=0.0, self_speech=dict(own_voice=0.0)),
         ["C2", "C3", "C5", "C5b", "C6", "C7"]),
        # IMU 半速（队列被拖住）：只有采样率这一条该挂。
        ("imu_half_rate", dict(imu_hz=50.0,
                               self_speech=dict(own_voice=0.85, duck_at_ms=300.0)),
         ["C4c"]),  # 五个相各一条 C4c，断言名都归到这个前缀
        # 演示模式：来源是 near_field、自己说话会下压——但**旁人说话也会下压**，
        # 所以 C9 必须挂，而且信息里要写明那是已知代价。
        ("demo_policy", dict(policy="near_field_demo", source="near_field",
                             self_speech=dict(own_voice=0.85, duck_at_ms=300.0),
                             other_speech=dict(own_voice=0.85, duck_at_ms=300.0)),
         ["C9"]),
        # 来源与策略不符（例如把代理说成骨传导）：C5b 必须抓住。
        ("source_mismatch", dict(policy="near_field_demo", source="bone",
                                 self_speech=dict(own_voice=0.85, duck_at_ms=300.0)),
         ["C5b"]),
    ]
    failures = 0
    for name, overrides, expected in scenarios:
        defaults = dict(baseline=dict(), self_speech=dict(), quiet_after=dict(),
                        motion=dict(motion=True), other_speech=dict(own_voice=0.05))
        global_policy = overrides.pop("policy", "bone_only")
        global_source = overrides.pop("source", None)
        global_imu = overrides.pop("imu_ok", True)
        global_gravity = overrides.pop("gravity", 9.81)
        global_hz = overrides.pop("imu_hz", 100.0)
        phases = []
        for phase_name, prompt, seconds in PHASES:
            kwargs = dict(defaults.get(phase_name, {}))
            kwargs.update(overrides.get(phase_name, {}))
            phases.append(make_phase(phase_name, seconds, imu_ok=global_imu,
                                     gravity=global_gravity, imu_hz=global_hz,
                                     policy=global_policy, source=global_source, **kwargs))
        checks, failed = analyse(phases)
        failing = sorted({c["check"].split()[0] for c in checks if not c["ok"]})
        ok = failing == sorted(expected)
        print(f"{'PASS' if ok else 'FAIL'} 自测[{name}]：预期失败 {sorted(expected) or '无'}，"
              f"实得 {failing or '无'}")
        for item in failed:
            print(f"     {item}")
        if not ok:
            failures += 1
    print(f"\n检查器自测：{'全部符合预期' if failures == 0 else f'{failures} 个场景与预期不符'}")
    return 1 if failures else 0


def print_report(phases: list[Phase], checks: list[dict]) -> None:
    print(f"\n{'相':<14}{'帧数':>6}{'own峰值':>9}{'相关p50':>9}{'最低media':>10}{'下压帧':>8}{'运动帧':>8}{'重力':>8}{'延迟ms':>8}")
    for phase in phases:
        s = phase.summary()
        gravity = "—" if s["gravity_p50"] is None else f"{s['gravity_p50']:.2f}"
        latency = "—" if s["duck_latency_ms"] is None else f"{s['duck_latency_ms']:.0f}"
        print(f"{s['phase']:<14}{s['frames']:>6}{s['own_voice_peak']:>9.2f}{s['correlation_p50']:>9.2f}"
              f"{s['media_min']:>10}{s['ducked_frames']:>8}{s['motion_frames']:>8}{gravity:>8}{latency:>8}")
    print()
    for check in checks:
        print(f"{'✅' if check['ok'] else '❌'} {check['check']}：{check['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="真机验证 Apple 式对话感知")
    parser.add_argument("--port", help="串口，例如 /dev/cu.usbmodem1101")
    parser.add_argument("--file", help="离线分析：一个每行 JSON 的抓包日志")
    parser.add_argument("--out", default="记录/对话感知验证", help="报告目录")
    parser.add_argument("--dry-run", action="store_true", help="只检查串口与固件版本，不跑相")
    parser.add_argument("--skip-other-speech", action="store_true", help="没有人配合时跳过旁人说话相")
    parser.add_argument("--selftest", action="store_true",
                        help="用合成数据验证检查器本身（不需要设备、不需要人）")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.port and not args.file:
        parser.error("需要 --port 或 --file")

    phases: list[Phase] = []

    if args.file:
        phase = Phase("offline", "", 0.0)
        with open(args.file, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "earbud_command" not in line:
                    continue
                parsed = parse_line(line)
                if parsed.value is None or not hasattr(parsed.value, "media_percent"):
                    continue
                f = parsed.value
                phase.frames.append(Frame(
                    t_ms=float(len(phase.frames)) * TICK_MS, schema=f.schema_version,
                    media_percent=f.media_percent, conversation=f.conversation,
                    confidence=f.confidence, own_voice_confidence=f.own_voice_confidence,
                    own_voice_correlation=f.own_voice_correlation, own_voice_source=f.own_voice_source,
                    imu_ok=f.imu_ok, imu_motion=f.imu_motion, imu_gravity=f.imu_gravity,
                    imu_drops=None, imu_samples=f.imu_samples,
                    evidence_policy=f.evidence_policy, problems=parsed.problems))
        phases = [phase]
    else:
        try:
            import serial  # noqa: PLC0415
        except ImportError:
            print("❌ 缺少 pyserial。用仓库自带的解释器：./.venv/bin/python tools/verify_conversation_awareness.py ...")
            return 3
        try:
            probe = serial.Serial(port=args.port, baudrate=BAUD, timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 打不开串口 {args.port}：{exc}")
            print("   先让出串口：CORES3_PORT=%s sh tools/free_port.sh --kill" % args.port)
            return 3
        probe.dtr = True
        probe.rts = True
        try:
            # 先读两秒，确认固件版本：不是 v3 就别跑了，跑了也全是假结论。
            started = time.monotonic()
            schema_seen = set()
            own_voice_seen = False
            while time.monotonic() - started < 2.0:
                raw = probe.readline()
                if not raw or "earbud_command" not in raw.decode("utf-8", errors="replace"):
                    continue
                parsed = parse_line(raw.decode("utf-8", errors="replace"))
                if parsed.value is None or not hasattr(parsed.value, "media_percent"):
                    continue
                schema_seen.add(parsed.value.schema_version)
                own_voice_seen = own_voice_seen or parsed.value.own_voice_confidence is not None
            print(f"设备 schema={sorted(schema_seen)}，自身语音遥测{'有' if own_voice_seen else '没有'}")
            if not own_voice_seen:
                print("❌ 设备固件不是 v3：先烧分支固件（bash tools/build.sh && bash tools/flash.sh）")
                return 2
        finally:
            probe.close()

        if args.dry_run:
            print("✅ 干跑通过：串口可用、固件是 v3。加 --port 再跑一次就是完整验证。")
            return 0

        todo = [(n, p, s) for (n, p, s) in PHASES if not (args.skip_other_speech and n == "other_speech")]
        for name, prompt, seconds in todo:
            phase = Phase(name, prompt, seconds)
            print(f"\n=== {name}（{seconds:.0f}s）=== {prompt}")
            say(prompt)
            time.sleep(1.0)
            started = time.monotonic() * 1000.0
            read_frames(args.port, phase, started)
            phases.append(phase)

    checks, failures = analyse(phases)
    print_report(phases, checks)

    out_dir = REPO_ROOT / args.out / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "port": args.port,
        "source_file": args.file,
        "phases": [p.summary() for p in phases],
        "checks": checks,
        "failures": failures,
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out_dir / 'report.json'}")
    if failures:
        print(f"\n❌ {len(failures)} 条断言失败：")
        for item in failures:
            print(f"  · {item}")
        return 1
    print("\n✅ 全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
