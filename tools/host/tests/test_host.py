#!/usr/bin/env python3
"""上位机自测：不需要设备、不需要串口、不需要浏览器。

与仓库既有的主机单测风格一致（无第三方框架、断言计数、失败即非零退出）：

    python3 tools/host/tests/test_host.py

覆盖的是**上位机唯一有真实逻辑的三层**：协议解析、状态归约、模拟源。
界面本身不在这里测——界面是渲染，不是判断；判断错了才会让数字说谎。

另外有一组**针对真机日志的回归断言**（`fixtures/device_real.log` 是 2026-10-14
从设备上抓下来的原文）。它的作用很具体：解析器以后被改动时，必须仍然读得懂这台设备
真正吐出来的字节，而不是只读得懂测试里手写的 JSON。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tools/
sys.path.insert(0, str(ROOT))

from host.protocol import (  # noqa: E402
    CONTEXT_CLASSES,
    DEMO_SPEECH_GAIN_DB,
    MAX_NOISE_REDUCTION_DB,
    MAX_SPEECH_GAIN_DB,
    SCHEMA_VERSION,
    CaptureEvent,
    CommandFrame,
    LatencyReport,
    MicChunk,
    RouteEvent,
    d2,
    iter_json_objects,
    parse_line,
)
from host.mac_audio import parse_default_output, route_command  # noqa: E402
from host.server import (  # noqa: E402
    Broadcaster,
    CommandError,
    INDEX_FILE,
    build_command,
    pick_port,
)
from host.simulating import MockDevice  # noqa: E402
from host.state import (  # noqa: E402
    ENVIRONMENT_TEXT,
    STATE_TEXT,
    HostState,
    environment_text,
    link_text,
    self_check,
    state_text,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "device_real.log"

_passed = 0
_failed: list[str] = []


def check(condition: bool, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(label)


def eq(actual, expected, label: str) -> None:
    check(actual == expected, f"{label}（期望 {expected!r}，实得 {actual!r}）")


def close(actual: float, expected: float, label: str, tolerance: float = 1e-9) -> None:
    check(abs(actual - expected) <= tolerance, f"{label}（期望 {expected}，实得 {actual}）")


# --------------------------------------------------------------------------- 解析


def test_d2_matches_c_printf() -> None:
    # 固件用 printf("%.2f")，Python 的 round() 是银行家舍入：0.125 → 0.12 vs C 的 0.13。
    # 面板数字和串口日志必须一致，否则同一个量会被当成两个测量。
    eq(d2(0.125), 0.13, "d2 用四舍五入而不是银行家舍入")
    eq(d2(-0.125), -0.13, "d2 负数同样四舍五入")
    eq(d2(3.2), 3.2, "d2 保持两位以内")
    eq(d2(4.109), 4.11, "d2 常规进位")
    eq(d2(0.0), 0.0, "d2 零")


def test_mac_default_output_route() -> None:
    report = """Audio:\n\n    Devices:\n\n        MacBook Air扬声器:\n\n          Output Channels: 2\n\n        AirPods Pro:\n\n          Default Output Device: Yes\n          Output Channels: 2\n"""
    eq(parse_default_output(report), "AirPods Pro", "识别 Mac 默认音频输出")
    eq(route_command("AirPods Pro"), b"route active AirPods Pro\r\n", "生成路由回传命令")
    eq(route_command(None), b"route inactive AirPods Pro\r\n", "AirPods 不是默认输出时明确未连接")
    parsed = parse_line('{"type":"route","state":"active","device":"AirPods Pro"}')
    check(isinstance(parsed.value, RouteEvent), "route 回执能解析")
    eq(parsed.error, None, "route 回执不再记为坏帧")


def test_command_frame_full() -> None:
    line = (
        '{"type":"earbud_command","schema":1,"sequence":42,"timestamp_ms":6300,'
        '"speech_gain_db":3.20,"noise_reduction_db":4.10,"media_percent":84,'
        '"state":"ADJUSTING","environment":"VOICE","confidence":0.92,"link":"simulated"}'
    )
    parsed = parse_line(line)
    check(isinstance(parsed.value, CommandFrame), "完整帧解析为 CommandFrame")
    frame = parsed.value
    eq(frame.sequence, 42, "序号")
    eq(frame.timestamp_ms, 6300, "时间戳")
    close(frame.speech_gain_db, 3.20, "人声增益")
    # 注意：固件报的是 4.10，语义是"降噪量"的正数写法。README 的示例就是这个形状。
    close(frame.noise_reduction_db, 4.10, "旧固件的降噪量仍能读出（只为兼容历史日志）")
    eq(frame.media_percent, 84, "媒体音量")
    eq(frame.state, "ADJUSTING", "状态")
    eq(frame.environment, "VOICE", "环境")
    eq(frame.schema_version, 1, "历史帧保留 v1 schema")
    eq(frame.tuning, None, "旧固件的帧没有 eq_gain_db，tuning 应为 None")
    check(not frame.link_reported_connected, "simulated 不得被当作已连接")
    check(not parsed.problems, f"完整帧不应有异常（实得 {parsed.problems}）")


def test_new_firmware_frame_carries_tuning() -> None:
    """新固件（2026-10-14 起）不再发 noise_reduction_db，改发 24 频段 eq_gain_db。

    两条都要测：新帧读得懂、旧帧也读得懂——历史日志（含 fixtures 里那份真机节选）
    必须继续可回放，否则"把一次真有设备在场的会话搬回桌面复核"就断了。
    """
    gains = [0.0] * 24
    for band in range(8, 17):
        gains[band] = 4.2
    line = json.dumps(
        {
            "type": "earbud_command",
            "schema": 1,
            "sequence": 7,
            "timestamp_ms": 1000,
            "speech_gain_db": 4.2,
            "media_percent": 92,
            "state": "ADJUSTING",
            "environment": "NOISY",
            "confidence": 0.9,
            "link": "simulated",
            "eq_gain_db": gains,
        }
    )
    parsed = parse_line(line)
    check(not parsed.problems, f"新固件帧不应有异常（实得 {parsed.problems}）")
    frame = parsed.value
    eq(frame.noise_reduction_db, None, "新固件不再有降噪字段")
    check(frame.tuning is not None, "调音被解析出来")
    eq(frame.tuning.lifted_bands, 9, "抬起 9 个频段")
    close(frame.tuning.peak_gain_db, 4.2, "抬起幅度")
    check(frame.tuning.shape_is_speech_band, "形状是「只抬语音段」")

    state = HostState(source="serial")
    state.feed_line(line)
    snapshot = state.snapshot()
    tuning = snapshot["frame"]["tuning"]
    eq(tuning["lifted_bands"], 9, "快照里带上调音")
    check("noise_reduction_db" not in snapshot["frame"], "快照仍然不得出现降噪字段")
    eq(len(tuning["band_gain_db"]), 24, "24 个频段全都要到界面上")

    # 形状判断不能写死：真实的多段整定也要被判为"形状不规则"，而不是硬套语音段。
    tilted = json.dumps(
        {
            "type": "earbud_command", "schema": 1, "sequence": 8, "timestamp_ms": 1150,
            "speech_gain_db": 4.2, "media_percent": 92, "state": "ADJUSTING",
            "environment": "NOISY", "confidence": 0.9, "link": "simulated",
            "eq_gain_db": [0.0] * 5 + [3.0, 0.0, 2.0] + [0.0] * 16,
        }
    )
    sparse = parse_line(tilted).value
    check(not sparse.tuning.shape_is_speech_band, "不连续的抬升不应被判成语音段形状")


def test_context_model_frame() -> None:
    """融合第二轮（2026-10-16）起，帧里多了端侧情境模型的四个字段。

    形状照真机实发抄（见 plan-status 第 29 项的实测表）：有效帧报类别 + 置信度 + 一次前向
    的微秒数；窗没满时报 `context="unknown"` 且 `context_valid=false`。
    """
    line = (
        '{"type":"earbud_command","schema":2,"sequence":7,"timestamp_ms":700,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,'
        '"link":"simulated","context":"near_voice","context_score":0.873,'
        '"context_valid":true,"context_us":158}'
    )
    parsed = parse_line(line)
    check(not parsed.problems, f"有效情境帧不应有异常（实得 {parsed.problems}）")
    frame = parsed.value
    eq(frame.context, "near_voice", "情境类别原样读出（协议里永远是 ASCII 标识）")
    close(frame.context_score, 0.87, "置信度按两位小数读入")
    eq(frame.context_valid, True, "context_valid 读为布尔真")
    eq(frame.context_us, 158, "一次前向的耗时是设备实报的整数微秒")

    # 窗没满：占位类别 + valid=false。这一帧必须**没有**异常——它是正常的预热帧。
    warmup = (
        '{"type":"earbud_command","schema":2,"sequence":1,"timestamp_ms":100,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,'
        '"link":"simulated","context":"unknown","context_score":0.000,'
        '"context_valid":false,"context_us":0}'
    )
    warm = parse_line(warmup)
    check(not warm.problems, f"预热帧（窗没满）不应有异常（实得 {warm.problems}）")
    eq(warm.value.context_valid, False, "预热帧的 context_valid 为假")
    eq(warm.value.context, "unknown", "预热帧报占位类别")

    # 旧固件：一个情境字段都没有，也不能被当成缺字段。
    legacy = parse_line(
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":1.50,"noise_reduction_db":4.10,"media_percent":92,'
        '"state":"ADJUSTING","environment":"NOISY","confidence":0.90,"link":"simulated"}'
    )
    check(not legacy.problems, f"没有情境字段的旧帧不应有异常（实得 {legacy.problems}）")
    eq(legacy.value.context, None, "旧帧的 context 为 None（不是空串）")
    eq(legacy.value.context_valid, None, "旧帧的 context_valid 为 None：区分没报与报了假")


def test_own_voice_frame() -> None:
    """v3（2026-10-16）起，帧里多了自身语音证据。

    为什么必须有这几个字段：真机验证时要能分开回答"骨传导有没有给出证据"与"证据来了但
    状态机没下压"——这两件事的修法完全不同。所以 source 一并上报，别让上位机去猜
    confidence 是高还是低。
    """
    line = (
        '{"type":"earbud_command","schema":3,"sequence":9,"timestamp_ms":900,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":25,'
        '"state":"ADJUSTING","environment":"VOICE","confidence":0.90,'
        '"conversation":"LIKELY","link":"simulated",'
        '"own_voice_confidence":0.812,"own_voice_correlation":0.934,'
        '"own_voice_source":"bone","imu_ok":true,"imu_motion":false,'
        '"eq_gain_db":[0.0],"ambient_band_dbfs":[-60.0]}'
    )
    parsed = parse_line(line)
    check(not parsed.problems, f"有效 v3 帧不应有异常（实得 {parsed.problems}）")
    frame = parsed.value
    close(frame.own_voice_confidence, 0.81, "自身语音置信度按两位小数读入")
    close(frame.own_voice_correlation, 0.93, "相关系数读入")
    eq(frame.own_voice_source, "bone", "来源原样读出，不让上位机猜")
    eq(frame.imu_ok, True, "imu_ok 读为布尔真")
    eq(frame.imu_motion, False, "imu_motion 读为布尔假")

    # 近场代理：source 必须区分开。代理那条路已经在真机静音素材上误触发过，
    # 把它显示成"骨传导证据"就是在上位机上撒谎。
    fallback = parse_line(
        '{"type":"earbud_command","schema":3,"sequence":10,"timestamp_ms":1000,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"HOLDING","environment":"QUIET","confidence":0.50,'
        '"link":"simulated","own_voice_confidence":0.400,'
        '"own_voice_correlation":0.000,"own_voice_source":"near_field",'
        '"imu_ok":false,"imu_motion":false}'
    )
    check(not fallback.problems, f"代理帧不应有异常（实得 {fallback.problems}）")
    eq(fallback.value.own_voice_source, "near_field", "代理来源要能读出来")

    # 旧固件（v2）：不发这几个字段，读成 None，而不是 0.0。
    older = parse_line(
        '{"type":"earbud_command","schema":2,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,"link":"simulated"}'
    )
    check(not older.problems, f"v2 帧不应有异常（实得 {older.problems}）")
    eq(older.value.own_voice_confidence, None, "v2 帧的自身语音证据是 None：区分没报与报了 0")

    # v3 却缺字段：必须显式报出来，不能按 0.0 解读。
    broken = parse_line(
        '{"type":"earbud_command","schema":3,"sequence":11,"timestamp_ms":1100,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,"link":"simulated"}'
    )
    check(any("own_voice_confidence" in p for p in broken.problems),
          f"v3 缺自身语音字段要报异常（实得 {broken.problems}）")


def test_context_model_is_not_guessed() -> None:
    """设备报了自相矛盾的情境字段时要能被发现，不能静默当正常帧。

    这个项目里最贵的一类错误是"设备在说别的事，而上位机按自己的理解显示"：
    `valid=false` 却带一个真类别、类别不在那 9 个里、只报了一半字段——三种都要留下痕迹。
    """
    base = (
        '"type":"earbud_command","schema":2,"sequence":7,"timestamp_ms":700,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,"link":"simulated"'
    )
    contradiction = parse_line(
        "{" + base + ',"context":"music","context_score":0.900,"context_valid":false,"context_us":150}'
    )
    check(any("context_valid=false" in p for p in contradiction.problems),
          f"valid=false 带真类别要报异常（实得 {contradiction.problems}）")

    unknown_class = parse_line(
        "{" + base + ',"context":"siren","context_score":0.9,"context_valid":true,"context_us":150}'
    )
    check(any("未知情境类别" in p for p in unknown_class.problems),
          f"未知类别要报异常（实得 {unknown_class.problems}）")

    bad_score = parse_line(
        "{" + base + ',"context":"music","context_score":1.4,"context_valid":true,"context_us":150}'
    )
    check(any("情境置信度" in p for p in bad_score.problems),
          f"置信度越界要报异常（实得 {bad_score.problems}）")

    partial = parse_line("{" + base + ',"context":"music","context_valid":true}')
    check(any("只报了一部分" in p for p in partial.problems),
          f"只报一半字段要报异常（实得 {partial.problems}）")


def test_old_firmware_frame_still_reads() -> None:
    """旧固件的帧（带 noise_reduction_db、无 eq_gain_db）必须继续可读。

    这条挡的是"升级上位机之后，历史日志再也回放不了"——而回放是复核现场的唯一手段。
    """
    # 用手写的旧帧，**不用** fixtures/device_real.log：那份现在是当前固件（schema 2、
    # 带 24 段 eq_gain_db）的真机原文，拿它测"旧固件"就测错了对象。
    old_log = [
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":1.50,"noise_reduction_db":4.10,"media_percent":92,'
        '"state":"ADJUSTING","environment":"NOISY","confidence":0.90,"link":"simulated"}',
        '{"type":"earbud_command","schema":1,"sequence":2,"timestamp_ms":150,'
        '"speech_gain_db":2.00,"noise_reduction_db":4.30,"media_percent":90,'
        '"state":"ADJUSTING","environment":"VOICE","confidence":0.95,"link":"simulated"}',
    ]
    state = HostState(source="replay")
    for line in old_log:
        state.feed_line(line)
    snapshot = state.snapshot()
    eq(snapshot["stats"]["unparsed_json"], 0, "旧固件日志不得出现解析失败")
    eq(snapshot["stats"]["bad_lines"], 0, "旧固件日志不得出现坏行")
    eq(snapshot["stats"]["commands"], 2, "旧固件日志的帧数")
    check(snapshot["frame"]["tuning"] is None, "旧帧没有调音数据，界面据此显示推导值")
    check("noise_reduction_db" not in snapshot["frame"], "旧帧的降噪字段也不端给界面")


def test_command_frame_noise_reduction_sign() -> None:
    """降噪字段的符号必须按其真实语义判定：它是"降噪量的大小"（0…+8），不是负值偏移。

    固件的钳位是 `std::clamp(noise_reduction_db_, 0.0f, kMaxNoiseReductionDb)`
    （main/controller.cpp:307），所以校验是**符号敏感**的：负数即异常，超过 +8 即异常。
    设备屏幕上那句「降噪 −8 dB 上限」是自然语言说法，不是这个字段的符号——按它去要求
    负值，会把每一帧正常帧都标成异常（这正是这个测试第一版写错的地方）。
    """
    positive = parse_line(
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,"speech_gain_db":0,'
        '"noise_reduction_db":4.10,"media_percent":100,"state":"OBSERVING",'
        '"environment":"QUIET","confidence":0.5,"link":"simulated"}'
    )
    check(not positive.problems, f"降噪 4.10 属于正常范围，不应报异常（实得 {positive.problems}）")

    over = parse_line(
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,"speech_gain_db":0,'
        f'"noise_reduction_db":{MAX_NOISE_REDUCTION_DB + 0.5},"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.5,"link":"simulated"}'
    )
    check(bool(over.problems), "降噪超过硬上限必须报异常")

    # 校验线是**实发**上限（8.0），不是产品策略值（6.5）。固件末尾把值钳到
    # kDemoSpeechGainDb，所以 6.5…8.0 之间的帧是正常帧；旧断言拿 6.5 当上限，
    # 会把整个 demo 的正常动作报成异常（2026-10-14 对齐文档时发现）。
    policy_only = parse_line(
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,'
        f'"speech_gain_db":{MAX_SPEECH_GAIN_DB + 0.5},"noise_reduction_db":0,'
        '"media_percent":100,"state":"OBSERVING","environment":"QUIET",'
        '"confidence":0.5,"link":"simulated"}'
    )
    check(not policy_only.problems, "6.5…8.0 之间的增益是固件正常输出，不得报异常")

    over_gain = parse_line(
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,'
        f'"speech_gain_db":{DEMO_SPEECH_GAIN_DB + 0.1},"noise_reduction_db":0,'
        '"media_percent":100,"state":"OBSERVING","environment":"QUIET",'
        '"confidence":0.5,"link":"simulated"}'
    )
    check(bool(over_gain.problems), "人声增益超过 +8.0 dB 实发上限必须报异常")


def test_command_frame_rejects_bad_input() -> None:
    cases = {
        "缺 sequence": '{"type":"earbud_command","timestamp_ms":1,"speech_gain_db":0,'
        '"noise_reduction_db":0,"media_percent":100,"state":"OBSERVING",'
        '"environment":"QUIET","confidence":0.5,"link":"simulated"}',
        "字段类型错": '{"type":"earbud_command","sequence":"x","timestamp_ms":1,'
        '"speech_gain_db":0,"noise_reduction_db":0,"media_percent":100,"state":"OBSERVING",'
        '"environment":"QUIET","confidence":0.5,"link":"simulated"}',
        "坏 JSON": '{"type":"earbud_command",',
        "未知 type": '{"type":"something_else","x":1}',
    }
    for label, line in cases.items():
        parsed = parse_line(line)
        check(parsed.error is not None, f"{label} 必须报错而不是静默通过")
        check(parsed.value is None, f"{label} 不得产出值")

    # 媒体音量越界属于"能解析但数字不可信"，要保留值并报异常，而不是丢弃整帧。
    out_of_range = parse_line(
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,"speech_gain_db":0,'
        '"noise_reduction_db":0,"media_percent":140,"state":"OBSERVING",'
        '"environment":"QUIET","confidence":0.5,"link":"simulated"}'
    )
    check(isinstance(out_of_range.value, CommandFrame), "越界帧仍要能解析出值")
    check(bool(out_of_range.problems), "媒体音量 140% 必须报异常")
    eq(out_of_range.value.media_percent, 140, "越界时保留原始值，不静默钳掉")


def test_missing_schema_is_reported() -> None:
    """固件当前总发 schema。一旦不发了，说明协议在演进，按 v1 解读必须留痕。"""
    parsed = parse_line(
        '{"type":"earbud_command","sequence":1,"timestamp_ms":0,"speech_gain_db":0,'
        '"noise_reduction_db":0,"media_percent":100,"state":"OBSERVING",'
        '"environment":"QUIET","confidence":0.5,"link":"simulated"}'
    )
    check(isinstance(parsed.value, CommandFrame), "缺 schema 仍然解析出值")
    check(
        any("schema" in item for item in parsed.problems),
        f"缺 schema 必须留下痕迹（实得 {parsed.problems}）",
    )


def test_latency_missing_fields_are_reported() -> None:
    """n 缺失时不能静默变成 0——界面会把它解释成"对端未接通"，那是错误解释。"""
    trimmed = (
        '{"type":"latency","frames":10,"total_p50_ms":1.0,"total_p95_ms":2.0,'
        '"overhead_mean_us":1.0,"overhead_max_us":2.0,"overhead_samples":10,'
        '"stages":[{"stage":"capture","p50_ms":100.03,"p95_ms":100.04,"max_ms":100.05},'
        '{"stage":"transport","n":0,"p50_ms":0.0}]}'
    )
    parsed = parse_line(trimmed)
    check(isinstance(parsed.value, LatencyReport), "仍能解析出报告")
    check(
        any("缺 n" in item for item in parsed.problems),
        f"缺 n 必须报出来（实得 {parsed.problems}）",
    )
    check(
        any("p95_ms/max_ms" in item for item in parsed.problems),
        "缺 p95/max 也要报出来",
    )
    # 本地/对端阶段的标记必须正确：界面靠它决定怎么解释 0。
    by_name = {item.stage: item for item in parsed.value.stages}
    check(by_name["capture"].local, "capture 是本机实测阶段")
    check(not by_name["transport"].local, "transport 是对端上报阶段")


def test_unknown_enum_and_schema() -> None:
    line = (
        '{"type":"earbud_command","schema":2,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":0,"noise_reduction_db":0,"media_percent":100,'
        '"state":"DREAMING","environment":"VACUUM","confidence":0.5,"link":"simulated"}'
    )
    parsed = parse_line(line)
    eq(len(parsed.problems), 3, "schema/状态/环境各报一条异常")
    eq(parsed.value.schema_version, 2, "保留设备自称的 schema")
    eq(state_text("DREAMING"), "未知(DREAMING)", "未知状态不得被翻译成已知词")
    eq(environment_text("VACUUM"), "未知(VACUUM)", "未知环境不得被翻译成已知词")


def test_latency_report() -> None:
    line = FIXTURE.read_text(encoding="utf-8").splitlines()
    latency_lines = [item for item in line if '"type":"latency"' in item]
    check(bool(latency_lines), "真机日志里应当有 latency 汇总")
    parsed = parse_line(latency_lines[0])
    check(isinstance(parsed.value, LatencyReport), "latency 解析为 LatencyReport")
    report = parsed.value
    eq(report.frames, 7047, "汇总的帧数（真机原文）")
    close(report.overhead_mean_us, 1.53, "打点自身开销均值")
    capture = report.stage("capture")
    check(capture is not None, "必须能取到 capture 阶段")
    # 这份 fixture 是 2026-10-15 从**当前固件**重抓的真机原文（100 ms 节拍、schema 2）。
    # 断言原文值而不是"当前节拍"：这条测的是解析器读得懂设备真正吐出来的字节；
    # 以后换固件要重新抓一份，而不是改这里的数字来迁就。
    close(capture.p50_ms, 100.034, "真机原文原样读出（当前固件，100 ms 节拍）", tolerance=0.01)
    analyse = report.stage("analyse")
    close(analyse.p50_ms, 7.019, "24 频段分析 p50 与文档一致", tolerance=0.01)
    embed = report.stage("embed")
    eq(embed.samples, 101, "声纹阶段仍有采样计数")
    close(embed.p50_ms, 0.0, "声纹推理未实现，如实报 0")
    transport = report.stage("transport")
    eq(transport.samples, 0, "未接通的阶段样本数为 0，不能伪装成 0 ms")


def test_capture_and_mic_chunk() -> None:
    started = parse_line('{"type":"capture","state":"started","seconds":10,"rate":16000}')
    check(isinstance(started.value, CaptureEvent), "capture started 解析")
    eq(started.value.seconds, 10, "录音秒数")
    eq(started.value.rate, 16000, "录音采样率")

    bad_b64 = parse_line(
        '{"type":"mic_chunk","sequence":1,"samples":4,"rate":16000,"pcm":"!!!!"}'
    )
    check(bad_b64.error is not None, "坏 base64 必须报错")

    odd = parse_line('{"type":"mic_chunk","sequence":1,"samples":3,"rate":16000,"pcm":"AAAA"}')
    check(odd.error is not None, "PCM 字节数为奇数必须报错（不是 16 位样本）")

    # 用真实块大小：16 kHz 下 1024 样本 = 2048 字节（main/app_main.cpp 的
    # kCaptureChunkSamples）。手写一个"够小就行"的 base64 会掩盖长度相关的错误——
    # 最初这里写的是 12 个字符，解出来 7 字节（奇数），解析器按设计报了错。
    real_pcm = base64.b64encode(bytes(2048)).decode("ascii")
    ok = parse_line(
        '{"type":"mic_chunk","sequence":7,"timestamp_ms":123,"samples":1024,"rate":16000,'
        f'"pcm":"{real_pcm}"}}'
    )
    check(isinstance(ok.value, MicChunk), f"正常录音块解析（{ok.error}）")
    eq(ok.value.sequence, 7, "录音块序号")
    eq(ok.value.sample_count, 1024, "样本数")
    eq(len(ok.value.pcm), 2048, "PCM 字节数")
    check(not ok.problems, "如实报数的块不应有异常")

    mismatch = parse_line(
        '{"type":"mic_chunk","sequence":8,"timestamp_ms":1,"samples":4,"rate":16000,'
        f'"pcm":"{real_pcm}"}}'
    )
    check(bool(mismatch.problems), "自称 4 点实际 1024 点必须报异常")


def test_line_splitter() -> None:
    # 串口一次 read 不保证按行对齐：半行、多行、CRLF 都要能正确处理。
    stream = [b'{"a":1}\r', b'\n{"b":', b'2}\r\n{"c":3}', b'\r\n']
    lines = list(iter_json_objects(iter(stream)))
    eq(len(lines), 3, "三行")
    for index, item in enumerate(lines):
        payload = json.loads(item)
        eq(list(payload)[0], "abc"[index], f"第 {index + 1} 行内容")
    # 非 JSON 行（ESP-IDF 横幅）不产生错误，只是不产出值。
    banner = parse_line("I (1490) main_task: Returned from app_main()")
    check(banner.value is None and banner.error is None, "日志横幅既不是值也不是错误")


# --------------------------------------------------------------------------- 归约


def test_state_reduction_and_gaps() -> None:
    state = HostState(source="mock", detail="test")
    state.feed_line('{"type":"earbud_command","sequence":1,"timestamp_ms":150,"speech_gain_db":1.0,'
                    '"noise_reduction_db":2.0,"media_percent":95,"state":"OBSERVING",'
                    '"environment":"NOISY","confidence":0.9,"link":"simulated"}')
    snapshot = state.snapshot()
    eq(snapshot["stats"]["commands"], 1, "帧计数")
    eq(snapshot["frame"]["environment_text"], "噪声", "环境译成设备屏幕用的词")
    eq(snapshot["frame"]["state_text"], "正在感知", "状态译成设备屏幕用的词")
    eq(snapshot["frame"]["link_text"], "模拟", "simulated 必须显示为模拟")
    check(not snapshot["frame"]["link_reported_connected"], "simulated 不得显示为已连接")
    # 口径（2026-10-14 用户明确）：本产品只有增益与调音，没有降噪。
    # 固件仍会算一个 noise_reduction_db，但它不得再出现在界面拿到的快照里。
    check(
        "noise_reduction_db" not in snapshot["frame"],
        "快照不得把降噪量端到界面（本产品没有降噪这一项）",
    )
    check(
        "noise_reduction_db" not in snapshot["caps"],
        "快照的能力上限里也不该有降噪",
    )
    check("speech_gain_db" in snapshot["caps"], "增益上限要保留（界面画刻度用）")

    # 序号 1 → 4：丢 2 帧，必须报出来
    state.feed_line('{"type":"earbud_command","sequence":4,"timestamp_ms":600,"speech_gain_db":1.0,'
                    '"noise_reduction_db":2.0,"media_percent":95,"state":"ADJUSTING",'
                    '"environment":"VOICE","confidence":0.9,"link":"simulated"}')
    snapshot = state.snapshot()
    eq(snapshot["stats"]["gaps"], 1, "序号跳变计数")
    eq(snapshot["stats"]["frames_lost"], 2, "丢失帧数")
    check(any("序号跳变" in item["text"] for item in snapshot["problems"]), "丢帧要出现在异常列表里")
    check(any(item["kind"] == "state" for item in snapshot["events"]), "状态变化要进事件流")
    check(any(item["kind"] == "environment" for item in snapshot["events"]), "环境变化要进事件流")


def test_staleness_distinguishes_quiet_from_dead() -> None:
    """端口开着但没数据 != 正常。

    这条断言直接对应现场最容易误判的一种状态：设备拔掉后留下的陈旧节点能让端口打开，
    界面如果只看"端口打开了"就会一直显示一屏不动也不报错的数字。
    """
    state = HostState(source="serial")
    state.set_connection(True, "/dev/cu.usbmodem1101")
    # 旧实现没有宽限期：从未收到帧时 `is_stale()` 直接返回 `connected`，于是每次启动面板
    # 都会先弹一次假告警「已连接但 ? ms 没有收到任何帧」（数字位因为 age_ms() 返回 None
    # 只能显示 "?"）——两个字段的判据互相矛盾。2026-10-14 用户实测撞到。
    check(not state.is_stale(), "刚连上还没有任何帧，不得立刻报陈旧")
    check(not state.is_silent(), "刚连上不算静默")
    check(state.age_ms() is not None, "没有帧时年龄必须从连接那一刻算起，不能是 None")
    check(state.is_stale(time.monotonic() + 11.0), "连接后持续 10 秒以上没帧才判为陈旧")
    check(state.age_ms(time.monotonic() + 11.0) > 10_000, "陈旧时年龄要是个数字，界面才有得显示")

    state.feed_line('{"type":"earbud_command","sequence":1,"timestamp_ms":1,"speech_gain_db":0,'
                    '"noise_reduction_db":0,"media_percent":100,"state":"OBSERVING",'
                    '"environment":"QUIET","confidence":0.5,"link":"simulated"}')
    check(not state.is_stale(), "刚收到帧就不该判为陈旧")
    check(not state.is_silent(), "刚收到帧不算静默")
    # 5.9 秒静默：够不到"设备可能停了"，但绝不能写成"数据在流"。旧实现的告警区在这种
    # 空档里写的就是「数据在流，未发现异常。」——那句话正是用户报的 bug。
    check(not state.is_stale(time.monotonic() + 5.0), "短暂 5 秒停顿不误报设备死亡")
    check(state.is_silent(time.monotonic() + 5.0), "静默超过阈值必须报静默，不能说数据在流")
    check(state.is_stale(time.monotonic() + 11.0), "持续 10 秒以上没帧才判为陈旧")

    # 静默要单独记账。旧实现只在 0 < 间隔 < 5000 ms 时取样，≥5 秒的空档被直接丢掉，
    # 于是面板上 max 仍显示 101 ms，"上位机自己跟丢了 5.9 秒"完全看不见。
    state.last_frame_at = time.monotonic() - 5.9413
    state.feed_line('{"type":"earbud_command","sequence":2,"timestamp_ms":2,"speech_gain_db":0,'
                    '"noise_reduction_db":0,"media_percent":100,"state":"OBSERVING",'
                    '"environment":"QUIET","confidence":0.5,"link":"simulated"}')
    stats = state.snapshot()["stats"]
    check(stats["silences"] == 1, "5.9 秒静默必须单独记账，不能消失在统计里")
    check(5900 <= stats["max_silence_ms"] <= 6000, "最长静默要能被面板读出来")
    check(stats["interval"]["max"] < 1000, "静默不得污染节拍分布（p50/max）")

    # 录音时设备把节拍放宽到 250 ms，判陈旧的窗口要跟着放宽，否则界面会误报。
    state.feed_capture(CaptureEvent(state="started", seconds=10, rate=16000))
    check(state.recording, "录音状态置位")
    check(not state.is_stale(time.monotonic() + 1.0), "录音时 1 秒没帧不算陈旧")
    check(state.is_stale(time.monotonic() + 11.0), "录音时持续 10 秒以上没帧才算陈旧")


def test_capture_accounting() -> None:
    state = HostState(source="serial")
    state.feed_line('{"type":"capture","state":"started","seconds":10,"rate":16000}')
    eq(state.snapshot()["capture"]["text"], "录音中", "录音态文案")
    # 块内容必须用真实长度：8 个样本的手写 base64 解出来是奇数个字节，
    # 解析器会（正确地）拒绝它，于是这里测的就变成了"错误处理"而不是"计数"。
    block = base64.b64encode(bytes(2048)).decode("ascii")
    for sequence in (1, 2, 4):  # 第 3 块丢了
        state.feed_line(
            json.dumps(
                {
                    "type": "mic_chunk",
                    "sequence": sequence,
                    "samples": 1024,
                    "rate": 16000,
                    "pcm": block,
                }
            )
        )
    snapshot = state.snapshot()
    eq(snapshot["capture"]["chunks"], 3, "收到 3 块")
    eq(snapshot["capture"]["chunks_lost"], 1, "丢块必须被算出来")
    check(
        any("录音有断裂" in item["text"] for item in snapshot["problems"]),
        "丢块要明确说这段录音不能用于结论",
    )
    # 快照里的 audio_seconds 是两位小数的显示值，所以容差取 0.01，不要求浮点精确相等。
    close(snapshot["capture"]["audio_seconds"], 3 * 1024 / 16000, "录音时长按样本数算", tolerance=0.01)

    state.feed_line('{"type":"capture","state":"finished","chunks":3}')
    eq(state.snapshot()["capture"]["text"], "录音已完成", "录音完成文案")


def test_source_detail_can_be_filled_in() -> None:
    """端口是数据源启动时才定下来的，所以面板必须能在之后拿到它。

    真实症状：source_detail 一直是空串，于是一旦自动挑了端口、或端口被占后换了端口，
    界面上根本看不出它连的是哪一个。
    """
    state = HostState(source="serial", detail="")
    eq(state.snapshot()["source_detail"], "", "启动时还没有端口")
    state.set_source_detail("/dev/cu.usbmodem1101 @ 115200")
    eq(
        state.snapshot()["source_detail"],
        "/dev/cu.usbmodem1101 @ 115200",
        "解析端口后必须能回填给界面",
    )


def test_snapshot_sections_are_stable() -> None:
    """快照的分段被界面直接依赖，搬动字段就是破坏契约。"""
    state = HostState(source="mock")
    snapshot = state.snapshot()
    for key in ("source", "connected", "stale", "capture", "frame", "latency", "stats",
                "samples", "events", "problems", "raw_tail", "caps"):
        check(key in snapshot, f"快照必须含 {key} 段")
    for key in ("commands", "gaps", "frames_lost", "bad_lines", "interval"):
        check(key in snapshot["stats"], f"statistics 段必须含 {key}")
    for key in ("state", "text", "chunks", "chunks_lost", "audio_seconds"):
        check(key in snapshot["capture"], f"capture 段必须含 {key}")


def test_latency_fed_into_state() -> None:
    state = HostState(source="serial")
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        state.feed_line(line)
    snapshot = state.snapshot()
    check(snapshot["latency"] is not None, "真机日志喂进去后要有 latency")
    stages = {item["stage"]: item for item in snapshot["latency"]["stages"]}
    close(stages["analyse"]["p50_ms"], 7.019, "面板拿到的分析与真机原文一致", tolerance=0.01)
    eq(stages["embed"]["n"], 101, "声纹阶段有计数")
    check(snapshot["stats"]["unparsed_json"] == 0, "真机日志不应产生无法解析的 JSON")


def test_microphone_fault_is_visible() -> None:
    state = HostState(source="serial")
    good = ('{"type":"earbud_command","sequence":1,"timestamp_ms":0,"speech_gain_db":0,'
            '"noise_reduction_db":0,"media_percent":100,"state":"OBSERVING",'
            '"environment":"QUIET","confidence":0.5,"link":"simulated"}')
    fault = good.replace('"sequence":1', '"sequence":2').replace("OBSERVING", "MIC FAULT")
    state.feed_line(good)
    state.feed_line(fault)
    snapshot = state.snapshot()
    eq(snapshot["frame"]["state_text"], "麦克风异常", "麦克风异常要如实显示")
    check(any(item["kind"] == "mic" for item in snapshot["events"]), "麦克风失效要进事件流")


def _font_covered_codepoints() -> set[int]:
    """解出 lv_font_ui_16 真正覆盖的码点。

    这张表是 LV_FONT_FMT_CMAP_SPARSE_TINY：`unicode_list_1` 里存的是**相对 range_start
    的码点偏移**，取值落在这个区间内才是有效码点。第一版检查把偏移直接当成码点加了
    range_start 之外的东西，于是把 `调` `音` 这些明明在库里的字全判成缺字——
    检查错了比没有检查更糟，因为它会让人去"修"一个不存在的问题。
    """
    import re as _re3

    font = (ROOT.parent / "main" / "lv_font_ui_16.c").read_text(encoding="utf-8", errors="replace")
    cmaps = _re3.findall(
        r"\.range_start = (\d+), \.range_length = (\d+)[^}]*?\.list_length = (\d+)", font, _re3.S
    )
    covered: set[int] = set()
    for raw_start, raw_length, raw_list in cmaps:
        start, length, list_length = int(raw_start), int(raw_length), int(raw_list)
        if list_length == 0:
            # 密集区（ASCII）：整个区间都在。
            covered.update(range(start, start + length))
            continue
        for content in _re3.findall(r"static const uint16_t unicode_list_\d+\[\] = \{(.*?)\};", font, _re3.S):
            offsets = [int(x, 16) for x in _re3.findall(r"0x[0-9a-fA-F]+", content)]
            if len(offsets) != list_length:
                continue
            for offset in offsets:
                codepoint = start + offset
                if start <= codepoint < start + length:
                    covered.add(codepoint)
    return covered


def test_font_check_calibrates_against_known_characters() -> None:
    """先证明这个检查本身是对的，再拿它去管文案。

    README 里那份"缺字清单"把它自己列的几个字（`设` `度`）说错了，所以不能拿它当基准；
    基准用的是**生成字库时写进文件的 `--symbols` 清单**与人眼可核对的常识字对。
    """
    covered = _font_covered_codepoints()
    check(len(covered) > 100, f"字库覆盖的码点数应当是 100 以上（实得 {len(covered)}）")
    for ch in "端侧算法验证机人声增益调音媒体环境":
        check(ord(ch) in covered, f"{ch} 应当在字库里（校准）")
    # `段` 是本次差点踩的坑：调音卡片一度写 `9段 +4.2`，而它不在库。
    check(ord("段") not in covered, "段 应当被判为缺字（校准：它确实不在 115 个字形里）")


def test_firmware_ui_glyphs_are_in_the_font_subset() -> None:
    """设备屏幕上的每个中文字都必须在字库里。

    README 把这条写成纪律（"新增界面文案前必须先确认用字在字库内，缺字会显示为空白"），
    但它一直只靠人记。这个检查是差点踩坑之后加的；现在它挡得住"改了文案忘了查字库"。
    """
    import re as _re3

    covered = _font_covered_codepoints()
    ui = (ROOT.parent / "main" / "demo_ui.cpp").read_text(encoding="utf-8", errors="replace")
    # 只查**会画到屏幕上**的字符串：注解里的汉字是写给人看的，不该被这条约束管——
    # 本文件的注释里就要写清"段不在字库里"，把它一起禁掉等于禁止解释。
    drawn: list[str] = []
    for call in _re3.finditer(r"(?:lv_label_set_text|lv_label_set_text_fmt)\s*\(\s*[^,]+,\s*(.*?)\);", ui, _re3.S):
        drawn.extend(_re3.findall(r'"([^"\\]*)"', call.group(1)))
    # 卡片标题：make_card(..., "标题", &handle)
    for call in _re3.finditer(r"make_card\s*\(([^;]*?)\);", ui, _re3.S):
        quoted = _re3.findall(r'"([^"\\]*)"', call.group(1))
        if quoted:
            drawn.append(quoted[-1])
    check(len(drawn) > 5, f"应当扫到若干界面文案（实得 {len(drawn)} 条）")
    missing: list[str] = []
    for literal in drawn:
        for ch in literal:
            if ord(ch) < 0x80:
                continue
            if ord(ch) not in covered:
                missing.append(ch)
    eq(sorted(set(missing)), [], "设备界面文案里的每个非 ASCII 字都必须有字形")


def test_enum_maps_cover_known_values() -> None:
    eq(self_check(), [], "固件的状态与环境都必须有中文映射")
    eq(link_text("simulated"), "模拟", "simulated 文案")
    eq(link_text(None), "—", "没有帧时的占位")
    check("OBSERVING" in STATE_TEXT and "LOUD" in ENVIRONMENT_TEXT, "映射表内容")


# --------------------------------------------------------------------------- 模拟源


def test_mock_device_is_wire_compatible() -> None:
    """模拟源必须产出与真机同构的行，否则"用模拟源自测"就测不到真问题。"""
    device = MockDevice()
    lines: list[str] = []
    for step in range(1, 200):
        lines.extend(device.advance(step * 0.15))
    commands = [line for line in lines if '"earbud_command"' in line]
    check(len(commands) > 100, f"应当产出足够多的帧（实得 {len(commands)}）")

    state = HostState(source="mock")
    for line in lines:
        state.feed_line(line)
    snapshot = state.snapshot()
    eq(snapshot["stats"]["unparsed_json"], 0, "模拟帧必须全部可解析")
    eq(snapshot["problems"], [], "模拟帧不得产生异常")
    eq(snapshot["stats"]["gaps"], 0, "不注入丢帧时不得有跳变")
    check(snapshot["latency"] is not None, "模拟源要能产出 latency 汇总")

    # 硬上限：模拟值越界就等于在测试里纵容一个不该存在的状态。
    # 上限用**实发**值（8.0），不是产品策略值（6.5）——固件末尾钳到 kDemoSpeechGainDb。
    for frame in [json.loads(line) for line in commands]:
        check(frame["schema"] == SCHEMA_VERSION, "模拟帧必须是当前 schema")
        check(frame["speech_gain_db"] <= DEMO_SPEECH_GAIN_DB + 1e-9, "模拟增益不越实发上限")
        check(frame["external_voice_gain_db"] <= DEMO_SPEECH_GAIN_DB + 1e-9, "模拟人声意图不越上限")
        check("noise_reduction_db" not in frame, "模拟源不得再发已删除的降噪字段")
        check(len(frame["eq_gain_db"]) == 24, "模拟帧必须带满 24 段调音")
        # 四种模式各自的形状约束——模拟源最容易犯的错是"越大声抬越多"，
        # 而 safety_hold 恰恰禁止正增益，这条断言就是挡它的。
        if frame["mode"] == "SONG_EQ":
            check(max(frame["eq_gain_db"]) <= 4.0 + 1e-9, "song_eq 的逐段建议不超过 4 dB")
        elif frame["mode"] == "EXTERNAL_VOICE_INTENT":
            check(abs(max(frame["eq_gain_db"]) - frame["external_voice_gain_db"]) < 0.2,
                  "人声模式的曲线必须跟着标量走，不能一个动一个不动")
        elif frame["mode"] in ("FLAT", "SAFETY_HOLD"):
            check(max(frame["eq_gain_db"]) == 0.0, f"{frame['mode']} 不得给正增益")
        check(0 <= frame["media_percent"] <= 100, "模拟媒体音量在 0–100")
        check(frame["link"] == "simulated", "模拟源的 link 必须是 simulated")
        # 情境字段（融合第二轮）。最需要钉住的是**预热段**：真机在窗攒满 2 秒之前报
        # valid=false，模拟源要是跳过这一段，面板在 --mock 下就会显示一个真机不可能给出的
        # 类别——而"界面在假数据上看起来对"正是这类工具最容易骗人的地方。
        check("context_valid" in frame, "模拟帧必须带情境字段")
        if frame["timestamp_ms"] < 2000:
            check(frame["context_valid"] is False,
                  f"{frame['timestamp_ms']} ms 时窗还没满，context_valid 必须是 false")
            check(frame["context"] == "unknown", "窗没满时报占位类别 unknown")
        else:
            check(frame["context_valid"] is True, "窗满之后必须给出判断")
            check(frame["context"] in CONTEXT_CLASSES,
                  f"模拟类别必须在模型的 9 类里（实得 {frame['context']}）")
            check(0.0 <= frame["context_score"] <= 1.0, "模拟置信度在 [0,1]")
            check(frame["context_us"] > 0, "模拟前向耗时为正值")


def test_mock_can_reproduce_faults() -> None:
    """丢帧和麦克风失效必须可复现，否则上位机的告警没法被测到。"""
    dropping = MockDevice(drop_probability=0.5)
    lines: list[str] = []
    for step in range(1, 60):
        lines.extend(dropping.advance(step * 0.15))
    state = HostState(source="mock")
    for line in lines:
        state.feed_line(line)
    check(state.snapshot()["stats"]["frames_lost"] > 0, "注入丢帧后必须能被检出")

    faulty = MockDevice(plant_fault=True)
    state = HostState(source="mock")
    for step in range(1, 20):
        for line in faulty.advance(step * 0.15):
            state.feed_line(line)
    eq(state.snapshot()["frame"]["state_text"], "麦克风异常", "注入故障后必须显示麦克风异常")


def test_mock_recording_produces_chunks() -> None:
    device = MockDevice(recording=True)
    lines: list[str] = []
    for step in range(1, 20):
        lines.extend(device.advance(step * 0.15))
    state = HostState(source="mock")
    state.feed_line(json.dumps({"type": "capture", "state": "started", "seconds": 3, "rate": 16000}))
    for line in lines:
        state.feed_line(line)
    snapshot = state.snapshot()
    check(snapshot["capture"]["chunks"] > 0, "录音模式下要产出分块")
    eq(snapshot["capture"]["chunks_lost"], 0, "顺序产出时不得报丢块")
    check(snapshot["capture"]["audio_seconds"] > 0, "录音时长要有值")


# --------------------------------------------------------------------------- 真机回归


def test_real_device_log_parses_end_to_end() -> None:
    """对真机原文的回归：这是 2026-10-15 从当前固件实际抓下来的输出（100 ms、schema 2）。"""
    check(FIXTURE.exists(), f"真机日志存在（{FIXTURE}）")
    text = FIXTURE.read_text(encoding="utf-8")
    state = HostState(source="replay", detail=str(FIXTURE))
    for line in text.splitlines():
        state.feed_line(line)
    snapshot = state.snapshot()
    check(snapshot["stats"]["commands"] >= 40, f"真机日志里的帧数（实得 {snapshot['stats']['commands']}）")
    eq(snapshot["stats"]["gaps"], 0, "真机日志序号连续（实测无丢帧）")
    eq(snapshot["stats"]["unparsed_json"], 0, "真机日志没有解析不了的 JSON")
    eq(snapshot["stats"]["bad_lines"], 0, "真机日志没有坏行")
    check(snapshot["latency"] is not None, "真机日志里应当有 latency 汇总")
    close(snapshot["latency"]["overhead_mean_us"], 1.53, "打点开销均值")
    # 设备自称链路是模拟的——上位机不得把它显示成已连接。
    eq(snapshot["frame"]["link_text"], "模拟", "真机当前 link=simulated")
    check(
        all(item["kind"] != "mic" or "异常" not in item["text"] for item in snapshot["events"]),
        "真机日志里不应出现麦克风异常事件",
    )


# --------------------------------------------------------------------------- 主机端


def test_command_whitelist() -> None:
    """回控是唯一会往设备写字节的通路，只放行设备真正实现了的两条命令。

    放行边界不是"逐字相等"，而是**与设备侧解析等价**：固件用 `strncmp("rec start", 9)`
    加 `sscanf("%d")`，所以 `rec start5` 在设备上就是"录 5 秒"。上位机把它归一化成同一
    条命令行，才算如实反映设备会做什么。其余一切——包括带分号、换行、`$(...)` 的输入——
    必须整体拒绝，绝不"截断成合法命令"。
    """
    eq(build_command("rec stop"), "rec stop", "rec stop 原样通过")
    eq(build_command("REC STOP"), "rec stop", "大小写不敏感，但输出归一化")
    eq(build_command("rec start"), "rec start", "不带秒数＝一直录")
    eq(build_command("rec start 5"), "rec start 5", "带秒数")
    eq(build_command("  rec start   5  "), "rec start 5", "空白归一化")

    # "rec start5" 刻意**不在**拒绝列表里：设备侧的解析是 strncmp("rec start", 9) 加
    # sscanf("%d")，所以它和 "rec start 5" 在设备上完全等价。上位机把它归一化成同一
    # 条命令行，才叫"如实反映设备会做什么"；拒绝它反而是自作主张。
    eq(build_command("rec start5"), "rec start 5", "与设备侧 strncmp+sscanf 的行为一致")

    bad = [
        "", "   ", "rec", "help", "reboot", "rec start abc", "rec start 0",
        "rec start -1", "rec start 99999", "rec start 5; reboot", "rec start 5\nrec stop",
        "REC START 5 extra", "\x03", "rec\tstart 5", "rec  start 5", "rec star 5",
    ]
    for item in bad:
        try:
            build_command(item)
        except CommandError:
            check(True, f"拒绝 {item!r}")
        else:
            check(False, f"必须拒绝 {item!r}，但它通过了")


def test_command_rejects_injection_attempts() -> None:
    """注入尝试必须整体失败，而不是"截断成合法命令"。

    设备侧的解析是 strncmp + sscanf，前缀匹配会让 "rec start 5; reboot" 里的
    "5; reboot" 被 sscanf 读成 5 —— 那等于上位机替使用者猜意图。这里要求显式拒绝。
    """
    for payload in ("rec start 5; reboot", "rec start 5\r\nrec stop", "rec start $(id)"):
        try:
            build_command(payload)
        except CommandError:
            check(True, f"注入 {payload!r} 被拒绝")
        else:
            check(False, f"注入 {payload!r} 必须被拒绝")


def test_pick_port_falls_forward() -> None:
    """端口被占用时要继续跑，而不是退出——现场最常见的占用是上一次没退干净。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy = holder.getsockname()[1]
        chosen = pick_port(busy)
        check(chosen != busy, f"占用端口 {busy} 时换一个（实得 {chosen}）")
        check(busy < chosen <= busy + 9, "在向后 10 个的范围内找")


def test_broadcaster_publishes_snapshots() -> None:
    state = HostState(source="mock")
    broadcaster = Broadcaster(state, min_interval=0.0)
    key = broadcaster.subscribe()
    broadcaster.publish()
    snapshot = broadcaster.wait_for_change(key, timeout=0.05)
    check(snapshot is not None, "发布后订阅者能拿到快照")
    check("stats" in snapshot, "快照结构完整")
    broadcaster.unsubscribe(key)
    # 退订之后 publish 不该再抛错——用"publish 之后订阅者拿不到东西"来验，
    # 而不是写 check(True)（恒真断言只会把通过数灌水，什么都挡不住）。
    broadcaster.publish()
    check(
        broadcaster.wait_for_change(key, timeout=0.02) is not None,
        "wait_for_change 超时也要返回快照（用于区分设备停了与连接断了）",
    )
    check(broadcaster._slots.get(key) is None, "退订后不再占着槽位")


def test_web_assets_contract() -> None:
    """界面是被 HTTP 端直接吐出去的静态文件，它的元素 id 就是前后端契约。

    以前那种"改了个 id，界面悄悄少一块"的问题，靠这条断言挡在测试里。
    """
    check(INDEX_FILE.exists(), f"界面文件存在（{INDEX_FILE}）")
    html = INDEX_FILE.read_text(encoding="utf-8")
    for element in ("chart", "latency-body", "problems", "events", "raw", "stats-body",
                    "capture-text", "btn-rec-start", "btn-rec-stop", "rec-seconds",
                    "conn-dot", "conn-text", "source-text", "age", "gain", "media",
                    "link", "env", "state", "conf", "seq", "toast", "eq", "control-warn"):
        check(f'id="{element}"' in html, f"界面必须有 #{element}")
    check("/events" in html, "界面用 SSE 取数据")
    check("/api/command" in html, "界面通过 /api/command 回控")
    check("rec start" in html and "rec stop" in html, "界面只发白名单命令")
    check("未实现" in html, "n=0 的阶段必须标注未实现，不能显示成 0 ms")
    # 口径：本产品只有增益与调音，没有降噪。界面里不得再出现降噪那一格。
    check('id="nr"' not in html, "界面不得再有降噪卡片")
    # 口径（2026-10-14 用户明确）：本产品只有增益与调音，没有降噪。
    # 禁止的是"把降噪当成本产品的一个量来呈现"——标签、图例、卡片、曲线。
    # 不禁解释性句子：页脚必须能写明"本机不做降噪"，否则等于连解释一起禁止。
    import re as _re2

    body = html.split("<script>")[0]
    banned = _re2.findall(r'<(?:h2|div class="sub"|span)>\s*降噪', body)
    eq(banned, [], "降噪不得作为面板标签/图例/卡片出现")
    check("降噪量 dB" not in body, "趋势图图例里不得再有降噪量")
    check('id="nr"' not in html, "不得再有降噪指标格子")
    check('{ key: "n"' not in html, "趋势图不得再画降噪曲线")
    check("调音" in html, "界面要显示调音（24 频段 EQ）")

    # 界面脚本里 $("...") 引用的每个 id 都必须真的存在于 HTML 里。这条纯 Python 也能查，
    # 而且正好挡住最常见的界面故障：改了 id、忘了同步脚本，于是某一块悄悄不再更新。
    import re as _re

    script = _re.search(r"<script>(.*?)</script>", html, _re.S)
    check(script is not None, "界面有内联脚本")
    ids_in_html = set(_re.findall(r'id="([^"]+)"', html))
    ids_in_js = set(_re.findall(r'\$\("([^"]+)"\)', script.group(1)))
    missing = sorted(ids_in_js - ids_in_html)
    eq(missing, [], "脚本引用的元素 id 必须都存在于 HTML 里")


def test_server_endpoints() -> None:
    """真起一个服务器，按 HTTP 打一遍。界面坏掉最典型的形态是端点 404 或字段缺失。"""
    import json as _json
    import threading
    import urllib.request

    from host.server import make_server

    state = HostState(source="mock")
    broadcaster = Broadcaster(state, min_interval=0.0)
    device = MockDevice()
    for step in range(1, 40):
        for line in device.advance(step * 0.15):
            state.feed_line(line)

    writes: list[bytes] = []
    port = pick_port(18900)
    httpd = make_server(state, broadcaster, port, writer=lambda data: writes.append(data))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
            health = _json.loads(response.read())
        check(health["ok"] and health["source"] == "mock", "health 报告数据源")

        with urllib.request.urlopen(f"{base}/api/snapshot", timeout=5) as response:
            snapshot = _json.loads(response.read())
        check(snapshot["stats"]["commands"] > 30, "快照里有设备帧")
        check(snapshot["frame"]["link_text"] == "模拟", "快照保持 simulated 口径")

        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            page = response.read().decode("utf-8")
            check(response.headers["Content-Type"].startswith("text/html"), "首页是 HTML")
        check("<canvas" in page and "CoreS3 上位机" in page, "首页内容是面板")

        request = urllib.request.Request(
            f"{base}/api/command",
            data=_json.dumps({"command": "rec start 5"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # 假 writer 只会把字节记下来，设备当然不会回应。所以这里必须得到
        # 502 + acknowledged:false —— 这正是"写成功 != 设备照做"的验收点。
        # 如果这里回了 ok:true，说明界面又会把一个没生效的命令显示成已执行。
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            eq(exc.code, 502, "设备无应答时返回 502")
            result = _json.loads(exc.read())
        else:
            check(False, "设备无应答时不得返回成功")
        check(not result["ok"], "无应答时 ok=false")
        check(result.get("acknowledged") is False, "明确标注未获应答")
        check("没有任何回应" in (result.get("warning") or ""), "要说明是设备没回应，不是按钮失效")
        eq(writes, [b"rec start 5\r\n"], "写进设备的字节与设备侧解析格式一致")
        eq(
            state.snapshot()["control"]["unresponsive"],
            True,
            "状态层要把它标成回控降级",
        )

        bad_request = urllib.request.Request(
            f"{base}/api/command",
            data=_json.dumps({"command": "reboot"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad_request, timeout=5)
        except urllib.error.HTTPError as exc:
            eq(exc.code, 400, "非白名单命令返回 400")
        else:
            check(False, "非白名单命令必须被拒绝")
        eq(writes, [b"rec start 5\r\n"], "被拒绝的命令不得写入串口")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_interval_stats_and_percentile() -> None:
    """主机侧到达间隔的数值本身要有断言。

    README 里记的那次"主机侧间隔被读取粒度拖到 200 ms 上方"就是这个统计算出来的；以前只断言了
    key 存在，等于把最该被钉住的数字留空。
    """
    from host.state import _percentile

    eq(_percentile([], 0.5), 0.0, "空输入返回 0")
    eq(_percentile([7.0], 0.5), 7.0, "单值")
    eq(_percentile([1.0, 2.0, 3.0, 4.0], 0.5), 2.0, "p50 取最近的观测值")
    eq(_percentile([1.0, 2.0, 3.0, 4.0], 0.95), 4.0, "p95 落在尾部")

    state = HostState(source="mock")
    frame = ('{"type":"earbud_command","schema":1,"sequence":%d,"timestamp_ms":%d,'
             '"speech_gain_db":1.0,"noise_reduction_db":2.0,"media_percent":95,'
             '"state":"OBSERVING","environment":"NOISY","confidence":0.9,'
             '"link":"simulated"}')
    for index in range(1, 6):
        state.feed_line(frame % (index, index * 150))
    interval = state.snapshot()["stats"]["interval"]
    eq(interval["n"], 4, "5 帧产生 4 个间隔")
    check(0 < interval["p50"] < 5000, f"间隔是合理量级（实得 {interval['p50']}）")
    check(interval["min"] <= interval["p50"] <= interval["max"], "分位数单调")

    # 序号跳变要能同时反映在 gaps / frames_lost / max_gap 三个口径上
    state.feed_line(frame % (20, 3000))
    stats = state.snapshot()["stats"]
    eq(stats["gaps"], 1, "跳变计数")
    eq(stats["frames_lost"], 14, "丢帧总数（20-6）")
    eq(stats["max_gap"], 14, "最大连丢")


def test_audio_relay_budget_caps_backlog() -> None:
    """播放器不给反压时，必须由我们丢掉旧音频，否则延迟只增不减。

    背景是一次真机实测：ffplay 在无节流写入下 1.08 秒吃下 5 秒音频（吸收比 4.6×），
    也就是它不按实时速率反压。实时流的产生速率和播放速率相同，于是任何一次抖动造成的
    积压都会被永久保留——用户听到的声音只会越拖越晚，而不是稳定在某个延迟上。

    这里用 `cat` 当"照单全收"的播放器，就不需要声卡也能验证：灌进 5 秒音频后，真正写入
    播放器的量必须受预算限制，其余作为过期音频被丢掉。
    """
    import time as _time

    from host.audio_relay import AudioRelay

    relay = AudioRelay(rate=16_000, queue_chunks=256, budget_seconds=0.1,
                       player_command=["cat"])
    relay.start()
    try:
        chunk = bytes(320 * 2)  # 20 ms @ 16 kHz
        for _ in range(250):    # 5 秒音频，瞬间灌进去
            relay.feed(chunk, 16_000)
        _time.sleep(0.5)
        stats = relay.stats()
    finally:
        relay.stop()

    check(stats.chunks_stale > 0, "超出延迟预算的旧音频必须被丢掉")
    # 预算 0.1 s = 5 块，加上 0.5 s 等待窗口最多再放 25 块。留足余量只测量级：
    # 关键是不能出现"250 块全写进去"这种结果。
    check(stats.chunks_written <= 60,
          f"写入量必须受预算限制（实得 {stats.chunks_written} 块）")
    check(stats.chunks_stale >= 150,
          f"大部分过期音频应被丢弃（实得 {stats.chunks_stale} 块）")


def test_relay_measures_real_queue_depth_not_our_writes() -> None:
    """中继的延迟必须来自**播放端的回执**，不是"我们写进去多少"。

    这是这轮修复的核心。旧实现用 ffplay：它不给反压也不暴露播放位置，上位机只能假设
    "写进去就在按实时播"，于是任何一次卡顿造成的积压都永久留在里面且看不见。
    新的播放端（tools/relay/relaysink）把"已交付"与"已播完"两个计数都报出来。

    这里用一个**假播放端**（照抄真实 sink 的输出格式）验证上位机确实把那些数字接住了，
    并且没有把它们和"我们写了多少包"混成一个数。
    """
    import stat
    import tempfile
    import time as _time

    from host.audio_relay import AudioRelay

    with tempfile.TemporaryDirectory() as folder:
        fake = Path(folder) / "fakesink"
        fake.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' "
            "'{\"type\":\"relay\",\"state\":\"running\",\"device\":\"AirPods Pro\","
            "\"pinned\":true,\"queued_ms\":21.5,\"queued_frames\":344,"
            "\"dropped_ms\":180.0,\"drained_ms\":640.0,\"driver_ms\":170.7,"
            "\"est_out_ms\":192.2,\"received_s\":12.0,\"starved_ticks\":0,"
            "\"clipped_samples\":3,\"buffer_frames\":512,\"sample_rate\":16000}'\n"
            "cat > /dev/null\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        relay = AudioRelay(rate=16_000, sink_binary=str(fake))
        eq(relay.mode, "sink", "默认播放端是 sink")
        relay.start()
        try:
            deadline = _time.monotonic() + 2.0
            live = relay.live()
            while _time.monotonic() < deadline and not (live or {}).get("measurable"):
                _time.sleep(0.05)
                live = relay.live()
        finally:
            relay.stop()

    check(live is not None and live.get("measurable") is True,
          "播放端报出状态后 measurable 必须为真")
    eq(live.get("queued_ms"), 21.5, "排队深度取自播放端回执")
    eq(live.get("driver_ms"), 170.7, "驱动侧输出延迟原样带出")
    eq(live.get("est_out_ms"), 192.2, "合计延迟 = 排队 + 驱动侧")
    eq(live.get("dropped_ms"), 180.0, "播放端丢弃量单独一条，不和队列丢弃混报")
    eq(live.get("drained_ms"), 640.0, "启动排空与运行期丢弃分开报")
    eq(live.get("pinned"), True, "是否钉住设备要能看出来")
    check(live.get("stale") is False, "刚收到状态时不能标成过期")
    # 上位机自己那份计数是另一回事：它只有"我们写了多少包"，不是延迟。
    check(not hasattr(relay.stats(), "queued_ms"), "排队深度不来自上位机自己的写入计数")


def test_relay_reports_unmeasurable_instead_of_guessing() -> None:
    """ffplay 那条路测不到延迟，就必须说测不到——不许显示成 0。"""
    from host.audio_relay import PLAYER_FFPLAY, AudioRelay

    relay = AudioRelay(rate=16_000, player=PLAYER_FFPLAY)
    live = relay.live()
    eq(live["measurable"], False, "ffplay 不报告播放位置")
    check("ffplay" in live["note"], f"理由里必须点名播放端（实得 {live['note']}）")
    check("note" in live and live["note"], "不能只给一个空状态")


def test_relay_sink_missing_binary_says_how_to_build() -> None:
    """播放端没构建时要给出**下一步命令**，而不是一个 No such file。"""
    from host.audio_relay import AudioRelay, RelayError

    relay = AudioRelay(rate=16_000, sink_binary="/nonexistent/relaysink")
    try:
        relay.build_command()
    except RelayError as exc:
        check("tools/relay/build.sh" in str(exc),
              f"错误信息里要有构建命令（实得 {exc}）")
    else:
        check(False, "缺播放端时必须报 RelayError")


def test_relay_snapshot_reaches_the_panel() -> None:
    """中继读数必须进快照——不进快照的数字等于不存在。"""

    class FakeRelay:
        def live(self):
            return {"mode": "sink", "measurable": True, "queued_ms": 20.0}

    state = HostState(source="serial")
    check(state.snapshot()["relay"] is None, "没开中继时这一格是 None，不是 0")
    state.attach_relay(FakeRelay())
    relay_block = state.snapshot()["relay"]
    eq(relay_block["measurable"], True, "中继读数进了快照")
    eq(relay_block["queued_ms"], 20.0, "排队深度原样带出")


def test_main_starts_with_relay_attached() -> None:
    """main() 真的能起来，并且中继读数进了快照。

    这一组是被一次真事故补上的：`state.attach_relay(relay)` 一开始写在了 HostState 构造
    **之前**，结果是启动即 UnboundLocalError——而当时所有单测都是绿的，因为它们没有一个
    跑 main()。真机上表现为"面板起不来"，看了日志才发现。凡是 main() 里的装配顺序，
    都得有一次真的把它跑起来的测试。
    """
    import json as _json
    import stat
    import tempfile
    import threading
    import urllib.request

    from host.__main__ import main
    from host.server import pick_port

    with tempfile.TemporaryDirectory() as folder:
        fake = Path(folder) / "fakesink"
        fake.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' "
            "'{\"type\":\"relay\",\"state\":\"running\",\"device\":\"AirPods Pro\","
            "\"pinned\":true,\"queued_ms\":21.5,\"driver_ms\":170.7,"
            "\"est_out_ms\":192.2,\"dropped_ms\":0.0,\"drained_ms\":600.0}'\n"
            "cat > /dev/null\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

        from host import audio_relay as relay_module

        original_init = relay_module.AudioRelay.__init__

        def patched(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            kwargs.setdefault("sink_binary", str(fake))
            original_init(self, *args, **kwargs)

        relay_module.AudioRelay.__init__ = patched
        # main() 会装 SIGINT/SIGTERM 处理器，而 signal.signal 只能在主线程调用。
        # 这里测的是装配顺序，不是信号处理，所以临时把它换成空实现。
        import host.__main__ as main_module

        original_signal = main_module.signal.signal
        main_module.signal.signal = lambda *args, **kwargs: None  # noqa: ARG005
        port = pick_port(18940)
        thread = threading.Thread(
            target=main,
            args=(["--mock", "--quiet", "--no-browser", "--relay-audio",
                   "--no-player", "--web-port", str(port)],),
            daemon=True,
        )
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            health = None
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f"{base}/api/health", timeout=2) as response:
                        health = _json.loads(response.read())
                    break
                except Exception:  # noqa: BLE001 - 起来之前连接失败是正常的
                    time.sleep(0.2)
            check(health is not None and health.get("ok"), "main() 起来后 /api/health 是通的")
            # 播放端的第一行状态不是立刻可见的（实测约 0.7 s：进程起来 + 管道首读），
            # 所以这里等它，而不是抓一次快照就下结论。
            relay_block = None
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                with urllib.request.urlopen(f"{base}/api/snapshot", timeout=5) as response:
                    snapshot = _json.loads(response.read())
                relay_block = snapshot.get("relay")
                if relay_block and relay_block.get("measurable"):
                    break
                time.sleep(0.2)
            check(relay_block is not None, "快照里有 relay 块")
            check(bool(relay_block) and relay_block.get("measurable") is True,
                  f"中继读数可用（实得 {relay_block}）")
        finally:
            relay_module.AudioRelay.__init__ = original_init
            main_module.signal.signal = original_signal
            # main() 跑在自己的守护线程里，进程退出时它随之结束；这里只等它把端口放开，
            # 免得下一组测试挑到同一个端口。
            time.sleep(0.5)


def test_control_acknowledgement_is_tracked() -> None:
    """写成功 != 设备照做。"""
    state = HostState(source="serial")
    check(not state.snapshot()["control"]["unresponsive"], "没发过命令时不算降级")
    state.note_control_sent("rec start 5")
    state.note_control_result(False, "2.0 秒内没有 capture 事件")
    control = state.snapshot()["control"]
    eq(control["sent"], 1, "发送计数")
    eq(control["acknowledged"], 0, "没有应答")
    check(control["unresponsive"], "发了命令却零应答 → 必须判为降级")
    check(
        any("设备没有任何回应" in item["text"] for item in state.snapshot()["problems"]),
        "降级要出现在异常列表里",
    )

    state.note_control_sent("rec start 5")
    state.feed_capture(CaptureEvent(state="started", seconds=5, rate=16000))
    state.note_control_result(True, "")
    eq(state.snapshot()["control"]["acknowledged"], 1, "设备回应后计入应答")
    check(not state.snapshot()["control"]["unresponsive"], "有应答之后不再算降级")

    # 自动音频中继不是 HTTP 请求，没有同步等待器；capture started 本身就是硬件回执。
    relay_state = HostState(source="serial")
    relay_state.note_control_sent("rec start（音频中继）")
    relay_state.feed_capture(CaptureEvent(state="started", seconds=0, rate=16000))
    check(
        not relay_state.snapshot()["control"]["unresponsive"],
        "设备已开始录音时不得误报从未进入录音态",
    )


def test_serial_source_survives_transient_read_errors() -> None:
    """读串口出一次错不该终结整个会话。

    真实事故：上位机跑了 30 分钟后因 `device reports readiness to read but returned no
    data`（另一个进程抢了串口）整体退出，退出码 1。一个长会话不该被一次扰动终结，
    但也不能静默重连——重连次数与错误次数必须报到面板上。
    """
    import threading as _threading

    from host.sources import SerialSource

    good_frame = (
        '{"type":"earbud_command","schema":1,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":1.0,"noise_reduction_db":2.0,"media_percent":95,'
        '"state":"OBSERVING","environment":"NOISY","confidence":0.9,"link":"simulated"}\r\n'
    ).encode("utf-8")

    class FakeConnection:
        def __init__(self, plan):
            self.plan = list(plan)
            self.closed = False
            self.dtr = False
            self.rts = False

        @property
        def in_waiting(self):
            return 0

        def read(self, _size=1):
            # 计划耗尽之后返回空（等同读超时），而不是抛断言错——否则"读超时"会被
            # 当成又一次瞬态错误，把错误计数测成 2，掩盖真正要测的那一次。
            if not self.plan:
                time.sleep(0.01)
                return b""
            action = self.plan.pop(0)
            if isinstance(action, Exception):
                raise action
            return action

        def close(self):
            self.closed = True

    calls = {"n": 0}

    def factory(_port, _baud):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次连接：先正常给一帧，然后抛一次瞬态错误（模拟被别的进程抢串口）
            return FakeConnection([good_frame, OSError("device reports readiness to read but returned no data")])
        # 重连之后继续正常输出
        return FakeConnection([good_frame.replace(b'"sequence":1', b'"sequence":2')])

    state = HostState(source="serial")
    stop = _threading.Event()
    source = SerialSource(port=None, serial_factory=factory)
    # 绕过端口探测：直接给一个"已解析"的端口名
    source._resolved_port = "/dev/cu.usbmodem9999"

    def run():
        try:
            source.run(state, stop)
        except Exception:  # noqa: BLE001
            pass

    thread = _threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 8
    while time.time() < deadline:
        if state.reconnects > 0 and state.stats.commands >= 2:
            break
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=3)

    eq(state.read_errors, 1, "瞬态读取错误被记下")
    check(state.reconnects >= 1, "发生重连而不是退出")
    check(state.stats.commands >= 2, f"重连之后继续收到帧（实得 {state.stats.commands}）")
    check(
        any("串口读取出错" in item["text"] for item in state.snapshot()["problems"]),
        "瞬态错误要出现在异常列表里，不能静默重连",
    )


def test_sse_endpoint_streams() -> None:
    """SSE 端点以前从没被 HTTP 打过——界面全靠它，坏了只有现场才发现。"""
    import json as _json
    import threading
    import urllib.request

    from host.server import make_server

    state = HostState(source="mock")
    broadcaster = Broadcaster(state, min_interval=0.0)
    device = MockDevice()
    for step in range(1, 20):
        for line in device.advance(step * 0.15):
            state.feed_line(line)

    port = pick_port(18940)
    httpd = make_server(state, broadcaster, port, writer=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=5) as response:
            check(
                response.headers["Content-Type"].startswith("text/event-stream"),
                "SSE 的内容类型正确",
            )
            # 连上就先推一帧，界面因此不需要等设备下一帧才有内容
            first = response.readline().decode("utf-8")
            check(first.startswith("data: "), f"第一条是 data 行（实得 {first[:20]!r}）")
            _json.loads(first[len("data: "):])
            check(True, "首帧是合法 JSON 快照")
            second = response.readline().decode("utf-8")
            check(second.strip() == "", "SSE 事件以空行结尾")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_server_refuses_control_without_device() -> None:
    """mock/回放源没有设备可写，回控必须明确拒绝，而不是静默成功。"""
    import json as _json
    import threading
    import urllib.error
    import urllib.request

    from host.server import make_server

    state = HostState(source="mock")
    broadcaster = Broadcaster(state, min_interval=0.0)
    port = pick_port(18920)
    httpd = make_server(state, broadcaster, port, writer=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/command",
            data=_json.dumps({"command": "rec start"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            eq(exc.code, 409, "没有设备可写时返回 409")
            payload = _json.loads(exc.read())
            check(not payload["ok"], "返回 ok=false")
        else:
            check(False, "没有设备可写时不得返回成功")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


class _FakePlayerProcess:
    """够用的子进程桩：PlayerService.available 只看 poll()。"""

    def poll(self):  # noqa: ANN201 - 与 subprocess.Popen 同形
        return None


def test_player_decision_path_is_not_silently_dropped() -> None:
    """S3 的决策必须真的下发到播放器——这条通路静默失败过一次。

    当时 `eq` 不在命令白名单里：`apply_decision` 每 100 ms 抛 PlayerError、又被 `except`
    吞掉，现象是**面板全绿而两条曲线恒为 0**。所以这里两头都要钉住：
      * 面板不能自己发 `eq`（曲线形状必须来自设备那一帧，这是"谁在做决策"的唯一变量）；
      * 上位机自己必须发得出去，且失败原因要留下来给人看。
    """
    from host.player import PlayerError, PlayerService

    service = PlayerService(autostart=False)
    try:
        service.command("eq", bands=[0.0] * 24, depth=2.5, media_percent=100)
        check(False, "面板不得能自己下发 eq")
    except PlayerError:
        check(True, "面板不得能自己下发 eq（形状只能来自设备）")

    sent: list[dict] = []
    service._process = _FakePlayerProcess()  # type: ignore[assignment]
    service._send = lambda payload: sent.append(payload)  # type: ignore[assignment]

    bands = [0.0] * 24
    bands[8:17] = [5.0] * 9
    check(service.apply_decision(bands, 80.0), "决策要能真的发出去")
    eq(len(sent), 1, "只发一条 eq")
    eq(sent[0]["cmd"], "eq", "发的是 eq 命令")
    eq(len(sent[0]["bands"]), 24, "eq 必须带满 24 段")
    eq(sent[0]["bands"][8], 5.0, "频段值原样透传")
    eq(sent[0]["depth"], service.depth, "eq 带处方深度")
    eq(sent[0]["media_percent"], 80.0, "eq 带媒体音量")
    eq(service.last_error, "", "成功时不得留下错误")

    check(not service.apply_decision(bands, 80.0), "同一形状在节流窗口内不得重复下发")

    def boom(payload: dict) -> None:
        raise PlayerError("串口断了")

    service._send = boom  # type: ignore[assignment]
    check(not service.apply_decision([1.0] * 24, 100.0), "下发失败必须返回 False")
    check("串口断了" in service.last_error, "失败原因必须留下来给人看，不能吞掉")
    check("串口断了" in service.snapshot()["last_error"], "失败原因要进快照，面板才看得见")


def main() -> int:
    tests = [
        test_d2_matches_c_printf,
        test_mac_default_output_route,
        test_command_frame_full,
        test_new_firmware_frame_carries_tuning,
        test_context_model_frame,
        test_own_voice_frame,
        test_context_model_is_not_guessed,
        test_old_firmware_frame_still_reads,
        test_command_frame_noise_reduction_sign,
        test_command_frame_rejects_bad_input,
        test_missing_schema_is_reported,
        test_latency_missing_fields_are_reported,
        test_unknown_enum_and_schema,
        test_latency_report,
        test_capture_and_mic_chunk,
        test_line_splitter,
        test_state_reduction_and_gaps,
        test_staleness_distinguishes_quiet_from_dead,
        test_capture_accounting,
        test_source_detail_can_be_filled_in,
        test_snapshot_sections_are_stable,
        test_latency_fed_into_state,
        test_microphone_fault_is_visible,
        test_font_check_calibrates_against_known_characters,
        test_firmware_ui_glyphs_are_in_the_font_subset,
        test_enum_maps_cover_known_values,
        test_mock_device_is_wire_compatible,
        test_mock_can_reproduce_faults,
        test_mock_recording_produces_chunks,
        test_real_device_log_parses_end_to_end,
        test_command_whitelist,
        test_command_rejects_injection_attempts,
        test_pick_port_falls_forward,
        test_broadcaster_publishes_snapshots,
        test_web_assets_contract,
        test_server_endpoints,
        test_interval_stats_and_percentile,
        test_player_decision_path_is_not_silently_dropped,
        test_audio_relay_budget_caps_backlog,
        test_relay_measures_real_queue_depth_not_our_writes,
        test_relay_reports_unmeasurable_instead_of_guessing,
        test_relay_sink_missing_binary_says_how_to_build,
        test_relay_snapshot_reaches_the_panel,
        test_main_starts_with_relay_attached,
        test_control_acknowledgement_is_tracked,
        test_serial_source_survives_transient_read_errors,
        test_sse_endpoint_streams,
        test_server_refuses_control_without_device,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 测试里就是要抓住一切
            _failed.append(f"{test.__name__} 抛异常：{type(exc).__name__}: {exc}")

    total = _passed + len(_failed)
    if _failed:
        print(f"失败 {len(_failed)} / {total}")
        for item in _failed:
            print(f"  ✗ {item}")
        return 1
    print(f"上位机自测通过：{len(tests)} 组，{total} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
