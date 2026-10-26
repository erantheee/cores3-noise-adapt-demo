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
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tools/
sys.path.insert(0, str(ROOT))

from host.protocol import (  # noqa: E402
    CONTEXT_CLASSES,
    EVENT_VALID_CLASSES,
    DEMO_SPEECH_GAIN_DB,
    MAX_BAND_GAIN_DB,
    MAX_NOISE_REDUCTION_DB,
    MAX_SPEECH_GAIN_DB,
    MIN_BAND_GAIN_DB,
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
from host.mac_audio import (  # noqa: E402
    DEFAULT_EARPHONES,
    Earphone,
    match_earphone,
    parse_default_output,
    route_command,
)
from host.voiceprint import VoiceprintService  # noqa: E402
from host.server import (  # noqa: E402
    Broadcaster,
    CommandError,
    INDEX_FILE,
    build_command,
    make_server,
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
from host.ambient import AmbientError, AmbientService, RelayAmbient  # noqa: E402
from host.memo import PROMPT_MAX_CHARS, MemoStore  # noqa: E402
from host.assistant import AssistantService, _intervals_seconds  # noqa: E402
from host.training import TrainingWriter  # noqa: E402
from host.continuous_audio import ContinuousAudioWriter  # noqa: E402
from host.__main__ import (  # noqa: E402
    DialogueTurnGate,
    ambient_gate_allows,
    fresh_self_veto,
    llm_ambient_authorization,
    parse_args,
    parse_earphone,
    relay_reports_audio,
    voiceprint_allows_ambient,
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


def wait_for(predicate, timeout_s: float = 2.0) -> bool:
    """等待后台线程（ASR 派发、LLM 判定）达到条件；单测不能靠 sleep 猜时序。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


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
    report = """Audio:\n\n    Devices:\n\n        MacBook Air扬声器:\n\n          Output Channels: 2\n\n        Xiaomi 耳夹式耳机:\n\n          Default Output Device: Yes\n          Output Channels: 2\n"""
    eq(parse_default_output(report), "Xiaomi 耳夹式耳机", "识别 Mac 默认音频输出（OWS 耳夹）")
    eq(tuple(e.label for e in DEFAULT_EARPHONES), ("Xiaomi OWS", "AirPods Pro"), "默认同时支持两副耳机")
    eq(match_earphone("Xiaomi 耳夹式耳机").label, "Xiaomi OWS", "识别 Xiaomi 耳夹")
    eq(match_earphone("AirPods Pro").label, "AirPods Pro", "识别 AirPods Pro")
    eq(match_earphone("airpods pro 2").label, "AirPods Pro", "大小写不敏感、容忍型号后缀")
    eq(match_earphone("MacBook Air扬声器"), None, "扬声器不是耳机")
    eq(route_command("Xiaomi 耳夹式耳机"), b"route active Xiaomi OWS\r\n", "OWS 在位时 active 且显示短名")
    eq(route_command("AirPods Pro"), b"route active AirPods Pro\r\n", "TWS 在位时 active 且显示 AirPods Pro")
    eq(route_command("MacBook Air扬声器"), b"route inactive Earphone\r\n", "扬声器：未连接 + 回退名")
    eq(route_command(None), b"route inactive Earphone\r\n", "没有默认输出时明确未连接")
    eq(
        route_command("Bose QC Ultra", earphones=(Earphone("Bose", "Bose QC"),)),
        b"route active Bose QC\r\n",
        "自定义耳机注册表：加新耳机不改代码",
    )
    parsed = parse_line('{"type":"route","state":"active","device":"Xiaomi OWS"}')
    check(isinstance(parsed.value, RouteEvent), "route 回执能解析")
    eq(parsed.error, None, "route 回执不再记为坏帧")


def test_earphone_cli_parsing() -> None:
    import argparse

    eq(parse_earphone("Bose=Bose QC").label, "Bose QC", "解析 MATCH=LABEL")
    eq(parse_earphone(" Xiaomi = Xiaomi OWS ").match, "Xiaomi", "去掉两端空白")
    for bad in ("Xiaomi", "=Xiaomi OWS", "Xiaomi="):
        try:
            parse_earphone(bad)
        except argparse.ArgumentTypeError:
            continue
        check(False, f"坏参数应被拒绝：{bad!r}")
    try:
        parse_earphone("Xiaomi=耳夹")
    except argparse.ArgumentTypeError:
        pass  # 中文显示名会被 S3 字库渲染成方框，必须拒绝
    else:
        check(False, "非 ASCII 显示名必须被拒绝（设备字库是固定子集）")
    args = parse_args(["--mock"])
    eq(tuple(e.label for e in args.earphones), ("Xiaomi OWS", "AirPods Pro"), "不传时用默认两副")
    args = parse_args(["--mock", "--earphone", "Bose=Bose QC"])
    eq(tuple(e.label for e in args.earphones), ("Bose QC",), "传了就覆盖默认注册表")


def test_memo_persists_injects_and_finalizes() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        clock = [1000.0]
        memo = MemoStore(folder, now=lambda: clock[0])
        eq(memo.snapshot()["events"], 0, "新目录没有历史记忆")
        memo.record_decision(
            {"dialogue": "yes", "ambient": "open", "confidence": 0.9,
             "source": "llm", "reason": "两个人一问一答", "number": 1},
            scene="近处人声", environment="人声嘈杂",
        )
        memo.record_ambient(open=True, scene="近处人声", environment="人声嘈杂",
                            intent=True, authorization={"state": "fast_local", "allow": True})
        clock[0] += 1.0
        memo.record_ambient(open=False, scene="近处人声", environment="人声嘈杂",
                            intent=False, authorization={"state": "llm_veto", "veto": True},
                            duration_s=1.0)
        memo.record_correction(verdict="open_bad", scene="近处人声", reason="这不是在跟我说话")
        memo.sample_frame(scene="近处人声", environment="人声嘈杂", broadband_dbfs=-40.0)
        prompt = memo.prompt_context(scene="近处人声")
        check(prompt.startswith("【用户记忆】"), "记忆摘要带固定前缀")
        check(len(prompt) <= PROMPT_MAX_CHARS, f"记忆摘要不超过 {PROMPT_MAX_CHARS} 字（实得 {len(prompt)}）")
        check("近处人声" in prompt, "摘要里带场景")
        check("yes 1" in prompt, "摘要里带对话判定计数")

        # 收尾先写本次运行的会话摘要（摘要按"本次运行"计数，不跨重启合并）
        memo.finalize_session()
        sessions = Path(folder) / "sessions.jsonl"
        check(sessions.exists(), "收尾写 sessions.jsonl")
        summary = json.loads(sessions.read_text(encoding="utf-8").splitlines()[-1])
        eq(summary["ambient_opens"], 1, "会话摘要记录环境声开启次数")
        eq(summary["ambient_short_opens"], 1, "1 秒就关算一次可疑短开")
        eq(summary["ambient_veto_opens"], 1, "被 LLM 否决的开启单独计数")
        eq(summary["corrections"], 1, "会话摘要记录用户纠正")

        # 影子建议要求最小样本量（8）：先补足样本，再验证"记忆足够时才给方向"。
        for index in range(7):
            memo.record_decision(
                {"dialogue": "yes", "ambient": "close", "confidence": 0.8,
                 "source": "llm", "reason": "补充样本", "number": index + 2},
                scene="近处人声", environment="人声嘈杂",
            )
        advice = memo.shadow_advice(scene="近处人声")
        eq(advice["state"], "shadow", "样本足够时影子建议生效")
        eq(advice["ambient_bias"], "conservative", "1 秒就关 + 被否决 → 影子建议偏保守")
        eq(advice["conversation_prior"], "likely", "唯一样本判 yes → 对话先验偏 likely")

        # 换一个进程实例，模拟上位机重启：rolling 与 profile 必须从磁盘恢复
        clock[0] += 60.0
        restored = MemoStore(folder, now=lambda: clock[0])
        eq(restored.snapshot()["events"], memo.snapshot()["events"], "重启后事件从磁盘恢复")
        check(restored.prompt_context(scene="近处人声").startswith("【用户记忆】"),
              "重启后仍能注入记忆")


def test_memo_retention_and_decay() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        clock = [0.0]
        memo = MemoStore(folder, retention_days=1, half_life_days=1.0, now=lambda: clock[0])
        clock[0] = 100.0
        memo.record_decision({"dialogue": "yes", "number": 1}, scene="安静")
        clock[0] += 86400.0  # 一个半衰期
        memo.record_decision({"dialogue": "no", "number": 2}, scene="安静")
        entry = memo.profile_entry("安静")
        eq(round(entry["decisions"]["yes"], 2), 0.5, "旧计数按半衰期衰减到 0.5")
        eq(round(entry["decisions"]["no"], 2), 1.0, "新计数是 1")

        # 超过保留期的事件在重启加载时被清掉
        clock[0] += 2 * 86400.0
        aged = MemoStore(folder, retention_days=1, half_life_days=1.0, now=lambda: clock[0])
        eq(aged.snapshot()["events"], 0, "超过保留期的事件不再恢复（profile 保留并继续衰减）")


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


def test_signed_tuning_is_backward_compatible() -> None:
    """24 段调音是**有符号**的：正=抬、负=削。

    现场调音要"低频更结实、人声更靠前、高频更细、整体不糊"，新曲线一定会削掉糊的中低频与
    被噪声主导的频段。这里钉住三件事：
      1. 旧固件（只发 0…+4）下既有字段含义与行为**一个字都不变**——新增字段全部是"加"的；
      2. 负值端到端透传：band_gain_db 原样保留，cut_bands/max_cut_db/peak_abs_db 数得对；
      3. 越界的建议保留原始值并报异常（本项目的纪律：不许替固件钳成"看起来正常"）。
    """

    def frame(gains: list[float], sequence: int = 20) -> str:
        return json.dumps(
            {
                "type": "earbud_command",
                "schema": 4,
                "sequence": sequence,
                "timestamp_ms": 2000,
                "speech_gain_db": 0.0,
                "external_voice_gain_db": 0.0,
                "media_percent": 100,
                "state": "ADJUSTING",
                "environment": "NOISY",
                "confidence": 0.9,
                "link": "simulated",
                "eq_gain_db": gains,
            }
        )

    # ---- 1) 旧固件形状：既有字段与旧行为逐项不变 -------------------------------
    legacy_gains = [0.0] * 24
    for band in range(8, 17):
        legacy_gains[band] = 4.0
    legacy = parse_line(frame(legacy_gains)).value
    eq(legacy.tuning.lifted_bands, 9, "旧固件的抬起段数不变")
    close(legacy.tuning.peak_gain_db, 4.0, "旧固件的抬起峰值不变")
    eq(legacy.tuning.cut_bands, 0, "旧固件没有削减段（新字段默认 0）")
    eq(legacy.tuning.max_cut_db, 0.0, "旧固件最大削减为 0（新字段默认 0.0）")
    eq(legacy.tuning.has_cuts, False, "旧固件 has_cuts=false")
    close(legacy.tuning.peak_abs_db, 4.0, "旧固件峰绝对值=最大抬升")
    check(legacy.tuning.shape_is_speech_band, "旧固件的「只抬语音段」形状判断不变")

    # ---- 2) 有符号曲线：削中低频、抬人声与空气感 -------------------------------
    gains = [0.0] * 24
    gains[2], gains[3], gains[4] = -3.0, -2.5, -1.5  # 削糊的中低频
    for band in range(8, 13):
        gains[band] = 2.0  # 人声 presence 抬
    for band in range(20, 24):
        gains[band] = 1.5  # 空气感抬
    parsed = parse_line(frame(gains))
    check(not parsed.problems, f"有符号曲线不应被标异常（实得 {parsed.problems}）")
    tuning = parsed.value.tuning
    eq(len(tuning.band_gain_db), 24, "24 段全部保留")
    eq(tuning.band_gain_db[2], -3.0, "负增益原样保留（设备原始建议，不钳成 0）")
    eq(tuning.lifted_bands, 9, "只数正值：5 + 4 段")
    eq(tuning.cut_bands, 3, "只数负值：3 段在削")
    close(tuning.max_cut_db, -3.0, "最大削减取最小值")
    close(tuning.peak_gain_db, 2.0, "peak_gain_db 旧含义不变=各段最大（正）值")
    close(tuning.peak_abs_db, 3.0, "峰绝对值=抬/削里绝对最大")
    check(tuning.has_cuts, "has_cuts 为真")
    check(not tuning.shape_is_speech_band, "有削减就不是「只抬语音段」形状")

    state = HostState(source="serial")
    state.feed_line(frame(gains))
    snapshot_tuning = state.snapshot()["frame"]["tuning"]
    eq(snapshot_tuning["cut_bands"], 3, "快照带上削减段数")
    eq(snapshot_tuning["max_cut_db"], -3.0, "快照带上最大削减")
    eq(snapshot_tuning["peak_abs_db"], 3.0, "快照带上峰绝对值")
    eq(snapshot_tuning["has_cuts"], True, "快照带上 has_cuts")
    eq(snapshot_tuning["band_gain_db"][2], -3.0, "快照的 band_gain_db 保留负值")
    caps = state.snapshot()["caps"]["band_gain_db"]
    eq(caps["min"], MIN_BAND_GAIN_DB, "面板量程下限来自协议常量")
    eq(caps["max"], MAX_BAND_GAIN_DB, "面板量程上限来自协议常量")

    # ---- 3) 全是削的曲线：不能因为"没有正值"就落回「只抬语音段」 ---------------
    cuts_only = parse_line(frame([-2.0] * 24, sequence=21)).value.tuning
    eq(cuts_only.lifted_bands, 0, "没有抬升段")
    close(cuts_only.peak_gain_db, -2.0, "peak_gain_db 此时是那个众数（负数）")
    eq(cuts_only.cut_bands, 24, "24 段全在削")
    close(cuts_only.peak_abs_db, 2.0, "峰绝对值=2.0")
    check(not cuts_only.shape_is_speech_band, "全削曲线不是语音段形状（旧逻辑会误判为是）")

    # ---- 4) 越界建议：保留原值 + 报异常 ---------------------------------------
    out_of_range = parse_line(frame([-13.0, 11.0] + [0.0] * 22, sequence=22))
    check(bool(out_of_range.problems), "越界建议必须报异常")
    check(
        any("调音建议越界" in item for item in out_of_range.problems),
        f"异常里要点名调音越界（实得 {out_of_range.problems}）",
    )
    eq(out_of_range.value.tuning.band_gain_db[0], -13.0, "越界值保留原始值，不钳掉")
    eq(out_of_range.value.tuning.band_gain_db[1], 11.0, "越界正值同样保留")


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


def test_context_mixed_fields() -> None:
    """D46 混合环境识别：帧里追加 context_top2 / context_top2_score / context_mixed。

    三键与 context 组同纪律：要么全不报（旧固件），要么成组。mixed 是布尔、top2 必须是
    9 类之一、top2 分数在 [0,1]、top2 不能与 top1 相同。
    """
    base = (
        '"type":"earbud_command","schema":4,"sequence":7,"timestamp_ms":700,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"VOICE","confidence":0.80,'
        '"own_voice_confidence":0.05,"link":"simulated","context":"near_voice",'
        '"context_score":0.620,"context_valid":true,"context_us":158'
    )
    # 混合窗：near_voice + babble，top2 过门槛。
    mixed = parse_line(
        "{" + base + ',"context_top2":"babble","context_top2_score":0.210,"context_mixed":true}'
    )
    check(not mixed.problems, f"混合情境帧不应有异常（实得 {mixed.problems}）")
    eq(mixed.value.context_top2, "babble", "第二候选类别原样读出")
    close(mixed.value.context_top2_score, 0.21, "第二候选置信度按两位小数读入")
    eq(mixed.value.context_mixed, True, "context_mixed 读为布尔真")

    # 单标签窗：top2 很低、mixed=false。
    single = parse_line(
        "{" + base
        + ',"context":"quiet","context_score":0.930,'
        '"context_top2":"other","context_top2_score":0.040,"context_mixed":false}'
    )
    check(not single.problems, f"单标签情境帧不应有异常（实得 {single.problems}）")
    eq(single.value.context_mixed, False, "context_mixed 读为布尔假")

    # 预热帧：top2 报占位 unknown/0.0/false，与 context 组同构，不应有异常。
    warmup = parse_line(
        '{"type":"earbud_command","schema":4,"sequence":1,"timestamp_ms":100,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,'
        '"own_voice_confidence":0.05,"link":"simulated","context":"unknown",'
        '"context_score":0.000,"context_valid":false,"context_us":0,'
        '"context_top2":"unknown","context_top2_score":0.000,"context_mixed":false}'
    )
    check(not warmup.problems, f"预热帧的占位 top2 不应有异常（实得 {warmup.problems}）")
    eq(warmup.value.context_top2, "unknown", "预热帧报占位第二候选")

    # 旧固件：三个键都没有 → None，且不算缺字段。
    legacy = parse_line(
        '{"type":"earbud_command","schema":4,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":1.50,"external_voice_gain_db":0.00,"media_percent":92,'
        '"state":"ADJUSTING","environment":"NOISY","confidence":0.90,'
        '"own_voice_confidence":0.05,"link":"simulated"}'
    )
    check(not legacy.problems, f"旧帧没有混合键不应有异常（实得 {legacy.problems}）")
    eq(legacy.value.context_top2, None, "旧帧的 context_top2 为 None")
    eq(legacy.value.context_mixed, None, "旧帧的 context_mixed 为 None：区分没报与报了假")

    # 只报了一部分 → 异常（三键是一组）。
    partial = parse_line(
        "{" + base + ',"context_top2":"babble","context_top2_score":0.210}'
    )
    check(any("混合情境字段只报了一部分" in p for p in partial.problems),
          f"三键只报两个必须点名（实得 {partial.problems}）")

    # top2 不是 9 类之一 → 异常。
    unknown_top2 = parse_line(
        "{" + base + ',"context_top2":"siren","context_top2_score":0.210,"context_mixed":true}'
    )
    check(any("未知第二候选情境类别" in p for p in unknown_top2.problems),
          f"第二候选类别越界必须点名（实得 {unknown_top2.problems}）")

    # top2 分数越界 → 异常。
    bad_score = parse_line(
        "{" + base + ',"context_top2":"babble","context_top2_score":1.4,"context_mixed":true}'
    )
    check(any("第二候选置信度" in p for p in bad_score.problems),
          f"第二候选置信度越界必须点名（实得 {bad_score.problems}）")

    # top2 与 top1 相同 → 异常（第二名不该是第一名）。
    same_class = parse_line(
        "{" + base + ',"context_top2":"near_voice","context_top2_score":0.210,'
        '"context_mixed":true}'
    )
    check(any("与第一名相同" in p for p in same_class.problems),
          f"第二候选与第一名相同必须点名（实得 {same_class.problems}）")

    # valid=false 却报真类别 top2 → 异常（占位符应是 unknown）。
    contradiction = parse_line(
        "{" + base.replace('"context_valid":true', '"context_valid":false')
        + ',"context":"unknown","context_score":0.000,'
        '"context_top2":"music","context_top2_score":0.210,"context_mixed":true}'
    )
    check(any("context_valid=false 却报了第二候选" in p for p in contradiction.problems),
          f"valid=false 却报真类别 top2 必须点名（实得 {contradiction.problems}）")


def test_dual_mic_telemetry() -> None:
    """D47 双麦遥测：mic2_broadband_dbfs / mic_diff_db / mic_coherence / mic2_valid。

    四键一组（旧固件不发 → 全 None）；值域：电平 ≤ 0 dBFS、相关系数 ∈ [-1,1]、
    电平差 ∈ ±80 dB。valid=false 时报 -120/0/0，是合法占位不是异常。
    """
    base = (
        '"type":"earbud_command","schema":4,"sequence":7,"timestamp_ms":700,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"VOICE","confidence":0.80,'
        '"own_voice_confidence":0.05,"link":"simulated"'
    )
    near = parse_line(
        "{" + base + ',"mic2_broadband_dbfs":-33.4,"mic_diff_db":2.40,'
        '"mic_coherence":0.350,"mic2_valid":true}'
    )
    check(not near.problems, f"近场双麦帧不应有异常（实得 {near.problems}）")
    close(near.value.mic2_broadband_dbfs, -33.4, "第二路麦电平按两位小数读入")
    close(near.value.mic_diff_db, 2.40, "电平差按两位小数读入（L − R）")
    close(near.value.mic_coherence, 0.35, "相关系数按两位小数读入")
    eq(near.value.mic2_valid, True, "mic2_valid 读为布尔真")

    # 占位帧：valid=false 时报 -120/0/0，合法。
    placeholder = parse_line(
        "{" + base + ',"mic2_broadband_dbfs":-120.0,"mic_diff_db":0.00,'
        '"mic_coherence":0.000,"mic2_valid":false}'
    )
    check(not placeholder.problems, f"双麦占位帧不应有异常（实得 {placeholder.problems}）")
    eq(placeholder.value.mic2_valid, False, "mic2_valid 读为布尔假")

    # 旧固件：四键都没有 → 全 None，不算缺字段。
    legacy = parse_line(
        '{"type":"earbud_command","schema":4,"sequence":1,"timestamp_ms":0,'
        '"speech_gain_db":1.50,"external_voice_gain_db":0.00,"media_percent":92,'
        '"state":"ADJUSTING","environment":"NOISY","confidence":0.90,'
        '"own_voice_confidence":0.05,"link":"simulated"}'
    )
    check(not legacy.problems, f"旧帧没有双麦键不应有异常（实得 {legacy.problems}）")
    eq(legacy.value.mic2_broadband_dbfs, None, "旧帧的第二路电平为 None")
    eq(legacy.value.mic2_valid, None, "旧帧的 mic2_valid 为 None：区分没报与报了假")

    # 只报了一部分 → 异常（四键是一组）。
    partial = parse_line("{" + base + ',"mic2_broadband_dbfs":-33.4,"mic_diff_db":2.40}')
    check(any("双麦遥测字段只报了一部分" in p for p in partial.problems),
          f"双麦四键只报两个必须点名（实得 {partial.problems}）")

    # 值域越界 → 异常。
    bad_coh = parse_line(
        "{" + base + ',"mic2_broadband_dbfs":-33.4,"mic_diff_db":2.40,'
        '"mic_coherence":1.5,"mic2_valid":true}'
    )
    check(any("相关系数" in p for p in bad_coh.problems),
          f"相关系数越界必须点名（实得 {bad_coh.problems}）")
    bad_level = parse_line(
        "{" + base + ',"mic2_broadband_dbfs":3.0,"mic_diff_db":2.40,'
        '"mic_coherence":0.3,"mic2_valid":true}'
    )
    check(any("第二路麦电平" in p for p in bad_level.problems),
          f"第二路电平超过 0 dBFS 必须点名（实得 {bad_level.problems}）")
    bad_diff = parse_line(
        "{" + base + ',"mic2_broadband_dbfs":-33.4,"mic_diff_db":-95.0,'
        '"mic_coherence":0.3,"mic2_valid":true}'
    )
    check(any("双麦电平差" in p for p in bad_diff.problems),
          f"电平差超出 ±80 dB 必须点名（实得 {bad_diff.problems}）")


def test_event_frame() -> None:
    """端侧声音事件判断（main/sound_event.hpp）的 `event` / `event_valid`。

    三态必须分得开：没报（旧固件）/ 窗未满或刚撞击（valid=false）/ 有判断（valid=true）。
    把 valid=false 画成"安静"就是把"还没判断"说成"判断为安静"。
    """
    base = (
        '"type":"earbud_command","schema":4,"sequence":11,"timestamp_ms":1100,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,'
        '"own_voice_confidence":0.05,"link":"simulated"'
    )
    valid_line = "{" + base + ',"event":"keyboard","event_valid":true}'
    parsed = parse_line(valid_line)
    check(not parsed.problems, f"有效事件帧不应有异常（实得 {parsed.problems}）")
    eq(parsed.value.event, "keyboard", "事件类别原样读出（协议里永远是 ASCII 标识）")
    eq(parsed.value.event_valid, True, "event_valid 读为布尔真")

    # 撞击帧：valid=false + artifact。它不是"安静"，也不是"没判断出来"。
    knock = parse_line("{" + base + ',"event":"artifact","event_valid":false}')
    check(not knock.problems, f"撞击帧不应有异常（实得 {knock.problems}）")
    eq(knock.value.event, "artifact", "撞击帧的类别是 artifact")
    eq(knock.value.event_valid, False, "撞击帧的 event_valid 为假")

    # 旧固件：两个字段都没有 → None，而不是空串/默认 quiet。
    legacy = parse_line("{" + base + "}")
    check(not legacy.problems, f"没有事件字段的旧帧不应有异常（实得 {legacy.problems}）")
    eq(legacy.value.event, None, "旧帧的 event 为 None（区分没报与报了 quiet）")
    eq(legacy.value.event_valid, None, "旧帧的 event_valid 为 None")

    # 自相矛盾：valid=true 却报 artifact。
    contradiction = parse_line("{" + base + ',"event":"artifact","event_valid":true}')
    check(any("event_valid=true" in p for p in contradiction.problems),
          "artifact + valid=true 必须被协议层拒绝")
    # 只报一半。
    partial = parse_line("{" + base + ',"event":"keyboard"}')
    check(any("只报了一部分" in p for p in partial.problems),
          "只报 event 不报 event_valid 必须被协议层拒绝")

    # 快照层：中文译名与三态。
    state = HostState(source="test")
    state.feed_line(valid_line)
    frame = state.snapshot()["frame"]
    eq(frame["event"], "keyboard", "快照里的 event 是 ASCII 标识")
    eq(frame["event_text"], "敲键盘", "快照里给出中文译名")
    eq(frame["event_valid"], True, "快照里的 event_valid 保留三态")



def test_speech_gate_frame() -> None:
    """92 维窗上的说话/非说话二分类学生：只做遥测，字段缺省要能读旧日志。"""
    base = (
        '"type":"earbud_command","schema":4,"sequence":12,"timestamp_ms":1200,'
        '"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":100,'
        '"state":"OBSERVING","environment":"QUIET","confidence":0.80,'
        '"own_voice_confidence":0.05,"link":"simulated"'
    )
    line = "{" + base + ',"speech_gate":0.83,"speech_gate_valid":true,"speech_gate_us":9}'
    parsed = parse_line(line)
    check(not parsed.problems, f"有效 gate 帧不应有异常（实得 {parsed.problems}）")
    eq(parsed.value.speech_gate, 0.83, "说话 gate 分数原样读出")
    eq(parsed.value.speech_gate_valid, True, "speech_gate_valid 读为布尔真")
    eq(parsed.value.speech_gate_us, 9, "说话 gate 推理耗时读出")

    legacy = parse_line("{" + base + "}")
    check(not legacy.problems, f"旧固件没有 gate 字段也不该报错（实得 {legacy.problems}）")
    eq(legacy.value.speech_gate, None, "旧固件 speech_gate 为 None，不是 0")
    eq(legacy.value.speech_gate_valid, None, "旧固件 speech_gate_valid 为 None")



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
        '"own_voice_source":"bone","own_voice_policy":"bone_only",'
        '"imu_ok":true,"imu_motion":false,'
        '"eq_gain_db":[0.0],"ambient_band_dbfs":[-60.0]}'
    )
    parsed = parse_line(line)
    check(not parsed.problems, f"有效 v3 帧不应有异常（实得 {parsed.problems}）")
    frame = parsed.value
    close(frame.own_voice_confidence, 0.81, "自身语音置信度按两位小数读入")
    eq(frame.evidence_policy, "bone_only", "证据策略原样读出（bone_only）")
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
        '"own_voice_policy":"near_field_demo",'
        '"imu_ok":false,"imu_motion":false}'
    )
    check(not fallback.problems, f"代理帧不应有异常（实得 {fallback.problems}）")
    eq(fallback.value.own_voice_source, "near_field", "代理来源要能读出来")
    eq(fallback.value.evidence_policy, "near_field_demo",
       "演示模式必须能读出来——它和'骨传导挂了在兜底'是两件事")

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
    check(parsed.value.event_p50_us >= 0.0, "latency 带出事件判断耗时（缺字段按 0）")
    check(parsed.value.context_p50_us >= 0.0, "latency 带出感知模型耗时（缺字段按 0）")
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
    for key in ("state", "text", "event_seq", "chunks", "chunks_lost", "audio_seconds"):
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
            check(max(frame["eq_gain_db"]) == 0.0,
                  "人声模式的歌曲 EQ 必须平直（D0：标量意图不是歌曲 EQ）")
        elif frame["mode"] in ("FLAT", "SAFETY_HOLD"):
            check(max(frame["eq_gain_db"]) == 0.0, f"{frame['mode']} 不得给正增益")
        # v4 环境声意图：模拟源必须与当前固件同构，否则 --mock 会掩盖一条坏链路。
        check(isinstance(frame.get("ambient_open"), bool), "模拟帧必须带 ambient_open 布尔意图")
        for key in (
            "broadband_dbfs",
            "voice_likelihood",
            "vocal_ratio",
            "spectral_flatness",
            "vocal_peakiness",
            "excited_vocal_bands",
        ):
            check(isinstance(frame.get(key), (int, float)),
                  f"模拟帧必须带判据遥测 {key}")
        if frame["environment"] == "VOICE":
            check(frame["ambient_open"] is True, "对话帧必须请求开放环境声")
        else:
            check(frame["ambient_open"] is False, "非对话帧不得请求开放环境声")
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
        # 声音事件判断（1 s 窗）。同样要钉住预热段：真机在窗攒满 1 秒之前报 valid=false。
        check("event_valid" in frame, "模拟帧必须带事件字段")
        if frame["timestamp_ms"] < 1000:
            check(frame["event_valid"] is False, "1 s 内窗还没满，event_valid 必须是 false")
            check(frame["event"] == "quiet", "窗没满时报占位类别 quiet")
        else:
            check(frame["event_valid"] is True, "窗满之后必须给出事件判断")
            check(frame["event"] in EVENT_VALID_CLASSES,
                  f"模拟事件类别必须合法（实得 {frame['event']}）")


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
                    "llm-state", "llm-task", "llm-asr", "llm-windows", "llm-summary", "llm-sessions",
                    "llm-decision", "llm-context",
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
    check("设备录音（控制设备自己的麦克风）" not in html, "主界面不得再把诊断采集当成大块产品功能")
    check("S3 负责录音" in html, "界面要明确 S3 承担 recording")
    check("LLM 工作台" in html, "上位机要显示 LLM 工作状态")
    check("快路径 → LLM 迟到否决" in html,
          "界面必须说明端侧快路径与 LLM 迟到否决的分工（D34）")
    check("LLM 上下文" in html, "界面必须显示 LLM 跨片段上下文")
    check("llm-decision" in html, "界面必须显示最近一次对话判定与环境声授权")

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


def test_voiceprint_only_does_not_report_relay_as_live() -> None:
    """只开 --voiceprint 时不能把 relay 说成"中继已开"（2026-10-23 真机审计）。

    main() 只在 --relay-audio / --ambient-executor relay|observe 时喂音频；只开
    --voiceprint 时建 Relay 只是为了触发设备持续采集（base_feed=None）。旧实现拿
    `relay is not None` 当"中继已开"，面板于是显示一个永远 0 字节的 24 ms 延迟，
    还会误报"声音不会进到你戴的耳机里"。这里钉住上报口径。
    """

    class FakeRelayAmbient:
        pass

    check(
        not relay_reports_audio(relay_audio=False, relay_ambient=None),
        "voiceprint-only：relay 对象存在也不得当中继已开上报",
    )
    check(
        relay_reports_audio(relay_audio=True, relay_ambient=None),
        "--relay-audio：必须上报中继已开",
    )
    check(
        relay_reports_audio(relay_audio=False, relay_ambient=FakeRelayAmbient()),
        "--ambient-executor relay/observe：必须上报中继已开",
    )


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

    # ---- 有符号（负=削）决策端到端 -------------------------------------------
    # 播放器已经支持 -12 dB（player.swift minAppliedGainDb）；上位机这条通路必须把负值
    # **原样**发出去，不能仍然按"只抬不削"把负段钳成 0。播放器自己会乘深度再钳位。
    service._send = lambda payload: sent.append(payload)  # type: ignore[assignment]
    service._last_decision = (0.0, [], 0.0)  # 清掉节流，确保这一发真的发出去
    signed = [0.0] * 24
    signed[2:5] = [-3.0, -2.5, -1.5]
    signed[8:13] = [2.0] * 5
    check(service.apply_decision(signed, 80.0), "含负增益的曲线也要能下发")
    eq(sent[-1]["bands"][2], -3.0, "负增益必须原样透传给播放器（不钳成 0）")
    eq(sent[-1]["bands"][8], 2.0, "正增益照旧")
    eq(service.last_error, "", "有符号曲线下发成功时不得留下错误")

    # 快照要能把"抬多少/削多少"分开报出来，面板才不必自己从数组里猜。
    from host.player import MAX_APPLIED_GAIN_DB as _PLAYER_MAX_DB

    service._process = _FakePlayerProcess()  # type: ignore[assignment]
    service._send = lambda payload: sent.append(payload)  # type: ignore[assignment]
    service._state = {
        "playing": True,
        "bypass": False,
        "peak_db": -20.0,
        "applied_bands": [-3.0, 0.0, 2.0] * 8,
        "source_bands": [-3.0, 0.0, 2.0] * 8,
        "depth": 2.5,
        "volume_percent": 150.0,
    }
    service._state_at = time.monotonic()
    snapshot = service.snapshot()
    close(snapshot["applied_peak_abs_db"], 3.0, "快照报出绝对值最大的那一段")
    close(snapshot["applied_max_cut_db"], -3.0, "快照报出最大削减（负数）")
    eq(snapshot["applied_lifted_bands"], 8, "快照报出被抬的段数（每 3 段 1 个正值 → 8）")
    eq(snapshot["applied_cut_bands"], 8, "快照报出被削的段数")
    close(snapshot["applied_peak_db"], 2.0, "旧字段 applied_peak_db 含义不变=各段最大值")
    eq(snapshot["min_applied_gain_db"], -12.0, "快照给出有符号量程下限（面板刻度用）")
    eq(snapshot["max_applied_gain_db"], _PLAYER_MAX_DB, "快照给出量程上限")


def test_eq_mode_and_bypass_control() -> None:
    """面板 EQ 三档（基础/激进/关闭EQ）的 HTTP 路由，2026-10-22。

    三档是为了 A/B 听感测试：mode 只属于 systemeq（demo 基线档位）；
    bypass（关闭EQ=执行端直通）对 systemeq 可用；没有执行端时必须明确拒绝，
    而不是"点过了"却什么都没发生。
    """
    from host.systemeq import SystemEqError, SystemEqService
    import threading
    import urllib.error
    import urllib.request

    class FakeSystemEq:
        def __init__(self) -> None:
            self.mode = "aggressive"
            self.sent: list[dict] = []

        def command(self, name: str, **fields):
            if name == "mode":
                value = fields.get("value")
                if value not in ("basic", "aggressive"):
                    raise SystemEqError("字段 value 只接受 basic / aggressive")
                self.mode = value
            self.sent.append({"cmd": name, **fields})
            return {"ok": True, "sent": {"cmd": name, **fields}}

        def snapshot(self):
            return {
                "available": True,
                "demo": True,
                "mode": self.mode,
                "bypass": False,
                "execution": "idle",
                "depth": 2.5,
            }

    def post(base: str, payload: dict):
        request = urllib.request.Request(
            f"{base}/api/player",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    state = HostState(source="test", detail={}, on_mic_chunk=None)
    broadcaster = Broadcaster(state, min_interval=0.0)
    systemeq = FakeSystemEq()
    httpd = make_server(state, broadcaster, pick_port(19000), systemeq=systemeq)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, result = post(base, {"cmd": "mode", "value": "basic"})
        eq(status, 200, "切基础档返回 200")
        check(result["ok"], "切基础档 ok")
        eq(systemeq.sent[-1], {"cmd": "mode", "value": "basic"}, "mode 命令原样透传给 systemeq")
        eq(result["systemeq"]["mode"], "basic", "回执快照带当前档位")

        status, result = post(base, {"cmd": "mode", "value": "loud"})
        eq(status, 400, "非法档位返回 400")
        check(not result["ok"] and "basic" in result["error"], "错误写明允许的档位")

        status, result = post(base, {"cmd": "bypass", "on": True})
        eq(status, 200, "关闭EQ（bypass）返回 200")
        eq(systemeq.sent[-1]["cmd"], "bypass", "bypass 透传给 systemeq")
        check(result["systemeq"] is not None, "回执快照带 systemeq")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    # 没有执行端：mode 与 bypass 都必须 409，不能假装点过了。
    httpd = make_server(state, broadcaster, pick_port(19010))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for payload in ({"cmd": "mode", "value": "basic"}, {"cmd": "bypass", "on": True}):
            status, result = post(base, payload)
            eq(status, 409, f"没有执行端时 {payload['cmd']} 返回 409")
            check(not result["ok"], "并如实报错")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    # systemeq 服务的字段校验：非法档位在发进程之前就被拒。
    service = SystemEqService(autostart=False)
    try:
        service.command("mode", value="loud")
        check(False, "非法档位必须被拒")
    except SystemEqError as exc:
        check("basic" in str(exc), "错误写明允许的档位")
    try:
        service.command("mode", value="basic")
        check(False, "没有进程时不得假装发送成功")
    except SystemEqError as exc:
        check("没有在运行" in str(exc), "合法档位也会因进程不在而如实报错")


def test_voiceprint_shadow_snapshot_is_harmless() -> None:
    """self/other 对话门控：未启用/缺依赖时必须安全，不得影响主链路。"""
    disabled = VoiceprintService(enabled=False)
    snap = disabled.snapshot()
    check(snap["mode"] == "self-veto", "声纹应是 self-veto 模式")
    check(snap["enabled"] is False and snap["ready"] is False, "未启用时不得 ready")
    check(snap["verdict"] is None and snap["windows"] == 0, "没有结果时不能编造判定")
    check(snap["threshold"] == 0.80, "默认阈值")
    # feed 在未 ready 时必须安全地丢弃，而不是把 None/属性错误抛进 mic 回调。
    disabled.feed(MicChunk(sequence=0, sample_count=1, rate=16000, timestamp_ms=0, pcm=b"\x00\x00"))
    check(disabled.snapshot()["fed_chunks"] == 0, "未 ready 时 feed 必须安全丢弃")

    class Fake:
        def snapshot(self):
            return {"ready": True, "verdict": "self", "score": 0.93, "mode": "self-veto"}

    state = HostState(source="test", detail={}, on_mic_chunk=None)
    state.attach_voiceprint(Fake())
    section = state.snapshot()["voiceprint"]
    check(section["verdict"] == "self" and section["mode"] == "self-veto", "声纹进快照")


def test_voiceprint_energy_activity_fallback() -> None:
    """S3 的 voice_likelihood 漏报"近场小声"时，声纹不能永远收到 speech_active=false。

    2026-10-22 真机：用户对着设备说话，设备端 voice_likelihood 只有 0.2；同一段音频
    离线喂 WavLM 的 self 相似度却是 0.95。根因是 activity_provider 只看 S3 判据，
    声纹 worker 的 speech_ratio 永远为 0，只能回 unknown。能量兜底用来解耦这两层。
    """
    import array

    class _Chunk:
        def __init__(self, pcm: bytes, rate: int = 16000) -> None:
            self.pcm = pcm
            self.rate = rate

    def synth(dbfs: float, count: int = 1600) -> _Chunk:
        amplitude = int(32768 * 10 ** (dbfs / 20.0))
        samples = array.array(
            "h", [amplitude if index % 2 == 0 else -amplitude for index in range(count)]
        )
        return _Chunk(samples.tobytes())

    check(abs(VoiceprintService._chunk_dbfs(synth(-20.0)) + 20.0) < 0.3,
          "块 RMS dBFS 要算得准")
    service = VoiceprintService(enabled=False)
    check(service.snapshot()["energy_floor_dbfs"] is None, "本底不足时不能编造能量本底")
    for _ in range(40):
        service._energy_history.append(VoiceprintService._chunk_dbfs(synth(-50.0)))  # type: ignore[attr-defined]
    floor = service.snapshot()["energy_floor_dbfs"]
    check(floor is not None and -51.0 < floor < -49.0, "本底估计要接近 -50 dBFS")
    check(VoiceprintService._chunk_dbfs(synth(-40.0)) >= floor + 6.0,
          "比本底高 10 dB 的块应能作为语音活动证据")
    check(VoiceprintService._chunk_dbfs(_Chunk(b"")) is None, "空块不得报活动")


def test_ambient_executor_and_intent_parsing() -> None:
    """环境声：设备只报意图，Mac 执行；意图与执行结果必须分开。

    这是本轮新增的链路（S3 预判对话 → ambient_open 意图 → Mac 切 AirPods 聆听模式）。
    测三件最容易骗人的事：旧帧没有字段不能当成 false；执行失败不能报成已开放；
    关闭时必须切回用户原来的聆听模式。
    """

    class _Completed:
        def __init__(self, code: int, out: str = "") -> None:
            self.returncode = code
            self.stdout = out
            self.stderr = ""

    # ---- 解析：v4 带 ambient_open；v3 没有 → None；类型不对 → 异常 ----
    payload = {
        "type": "earbud_command",
        "schema": 4,
        "sequence": 1,
        "timestamp_ms": 100,
        "speech_gain_db": 0.0,
        "external_voice_gain_db": 0.0,
        "media_percent": 60,
        "ambient_open": True,
        "state": "ADJUSTING",
        "environment": "VOICE",
        "mode": "FLAT",
        "conversation": "LIKELY",
        "confidence": 0.9,
        "execution": "decision_only",
        "link": "simulated",
    }
    frame = parse_line(json.dumps(payload)).value
    check(isinstance(frame, CommandFrame) and frame.ambient_open is True,
          "v4 帧必须解析出 ambient_open 意图")
    old_payload = dict(payload)
    old_payload.pop("ambient_open")
    old_payload["schema"] = 3
    old_frame = parse_line(json.dumps(old_payload)).value
    check(old_frame.ambient_open is None,
          "v3 没有 ambient_open：必须报 None，不能默认成 False")
    bad_payload = dict(payload)
    bad_payload["ambient_open"] = "yes"
    bad_parsed = parse_line(json.dumps(bad_payload))
    check(any("ambient_open" in problem for problem in bad_parsed.problems),
          "ambient_open 不是布尔值必须记为异常")

    # ---- 执行端：读原模式 → 切通透 → 关闭时切回 ----
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if argv[1] == "status":
            return _Completed(0, "AirPods Pro   电量：80%\n聆听模式：降噪\n")
        return _Completed(0, "✅ 已切到「通透模式」")

    service = AmbientService(Path(__file__), autostart=False, runner=runner)
    check(service.available, "测试 CLI 路径存在时执行端应可用")
    service.set_open(True)
    service.step()
    snapshot = service.snapshot()
    check(snapshot["open"] is True, "意图 open 后执行端报告已开放")
    check(snapshot["previous_mode"] == "anc", "切换前必须先读到原聆听模式")
    check(any(argv[1:] == ["mode", "transparency"] for argv in calls),
          "必须真的调用 mode transparency")
    service.set_open(False)
    service.step()
    check(service.snapshot()["open"] is False, "意图关闭后执行端报告已关闭")
    check(any(argv[1:] == ["mode", "anc"] for argv in calls),
          "关闭时必须切回切换前的模式")

    # ---- 已经是通透模式时不得重复切换 ----
    calls.clear()

    def already_runner(argv):
        calls.append(list(argv))
        return _Completed(0, "聆听模式：通透模式\n")

    already = AmbientService(Path(__file__), autostart=False, runner=already_runner)
    already.set_open(True)
    already.step()
    check(not any(argv[1:] == ["mode", "transparency"] for argv in calls),
          "已经是通透模式时不得重复切换")

    # ---- 执行失败：必须如实报错，并且在退避窗口内不刷屏 ----
    calls.clear()
    now = [100.0]

    def failing_runner(argv):
        calls.append(list(argv))
        if argv[1] == "status":
            return _Completed(0, "聆听模式：降噪\n")
        return _Completed(5, "❌ 打不开控制中心声音面板")

    failing = AmbientService(
        Path(__file__), autostart=False, runner=failing_runner, clock=lambda: now[0]
    )
    failing.set_open(True)
    failing.step()
    failed_snapshot = failing.snapshot()
    check(failed_snapshot["open"] is False, "执行失败时绝不能报已开放")
    check("打不开控制中心声音面板" in str(failed_snapshot["last_error"]),
          "失败原因必须留在快照里")
    mode_calls = len([argv for argv in calls if argv[1] == "mode"])
    failing.step()
    check(len([argv for argv in calls if argv[1] == "mode"]) == mode_calls,
          "失败退避窗口内不得每 100 ms 重试")
    now[0] += 4.0
    failing.step()
    check(len([argv for argv in calls if argv[1] == "mode"]) > mode_calls,
          "退避结束后必须重试")

    # ---- CLI 不存在：必须报不可用，不能假装 ----
    missing = AmbientService(Path("/nonexistent/airpods-cli"), autostart=False, runner=runner)
    missing.set_open(True)
    missing.step()
    missing_snapshot = missing.snapshot()
    check(missing_snapshot["available"] is False, "CLI 不存在时执行端必须报不可用")
    check("不存在" in str(missing_snapshot["last_error"]), "不可用原因必须可见")

    # ---- 快照：面板能同时看到设备意图与执行结果 ----
    state = HostState(source="serial")
    state.attach_ambient(service)
    check(state.snapshot()["ambient"] is not None, "环境声执行端必须进快照")


def test_ambient_gate_dual_turn_trigger() -> None:
    """2026-10-23 B 方案：声纹双向交替可作为触发源；自言自语不触发；LLM veto 优先。"""
    check(ambient_gate_allows(device_intent=False, llm_ok=False, self_ok=False,
                              intent_recent=False, evidence_recent=False,
                              dual_turn=True),
          "声纹双向交替（self+other）即使 S3 意图为 false 也放行")
    check(not ambient_gate_allows(device_intent=False, llm_ok=False, self_ok=True,
                                  intent_recent=False, evidence_recent=False,
                                  dual_turn=False),
          "只有 self（自言自语）不触发")
    check(not ambient_gate_allows(device_intent=False, llm_ok=False, self_ok=False,
                                  intent_recent=False, evidence_recent=False,
                                  dual_turn=True, llm_veto=True),
          "LLM 明确否决时 dual_turn 也不能放行（D34 迟到否决优先）")
    check(not ambient_gate_allows(device_intent=True, llm_ok=False, self_ok=False,
                                  intent_recent=False, evidence_recent=False,
                                  dual_turn=False),
          "S3 意图 + 无门证据时仍被门否决（门还是否决器）")


def test_ambient_gate_allows() -> None:
    """门是否决器：S3 意图是触发器；self/LLM 只批准，不发起；LLM veto 最高优先。

    2026-10-23 用户现场质疑"为什么我说话的时候把环境音拉起来"——self 证据不能
    独立触发环境声，只能批准 S3 已经发起的对话。
    """
    check(not ambient_gate_allows(device_intent=False, llm_ok=False, self_ok=True,
                                  intent_recent=False, evidence_recent=False),
          "你说话（self）不能触发环境声：S3 意图为 false 时必须关闭")
    check(ambient_gate_allows(device_intent=True, llm_ok=False, self_ok=True,
                              intent_recent=False, evidence_recent=False),
          "S3 意图 + 你接话时放行")
    check(ambient_gate_allows(device_intent=True, llm_ok=True, self_ok=False,
                              intent_recent=False, evidence_recent=False),
          "S3 意图 + LLM 确认时放行")
    check(ambient_gate_allows(device_intent=True, llm_ok=False, self_ok=False,
                              intent_recent=False, evidence_recent=True),
          "S3 意图 + 8 秒内门证据时放行（避免一句一关）")
    check(ambient_gate_allows(device_intent=True, llm_ok=False, self_ok=False,
                              intent_recent=True, evidence_recent=False),
          "S3 意图新鲜时放行")
    check(not ambient_gate_allows(device_intent=False, llm_ok=False, self_ok=False,
                                  intent_recent=False, evidence_recent=False),
          "没有任何证据时必须关闭")
    check(not ambient_gate_allows(device_intent=True, llm_ok=False, self_ok=False,
                                  intent_recent=False, evidence_recent=False),
          "S3 意图单独不足以放行（门必须监督）")
    check(not ambient_gate_allows(device_intent=True, llm_ok=False, self_ok=True,
                                  intent_recent=True, evidence_recent=True,
                                  llm_veto=True),
          "LLM 明确否决时即使 self 证据在场也不放行（D34 迟到否决优先）")


def test_relay_ambient_gate() -> None:
    """环境声走 S3 中继（当前 demo 路径）：门控必须真的拦住/放行，且状态可读。

    这条链路的危险性不在算法，而在"关着的时候以为开着"：设备会一直推 16 kHz 音频，
    执行端必须能证明自己确实在丢弃、确实在放行。失败/不可用也要如实上报。
    """

    class _FakeRelay:
        def __init__(self) -> None:
            self.failure = None
            self.fed: list[tuple[bytes, int]] = []

        def feed(self, pcm: bytes, rate: int) -> None:
            self.fed.append((pcm, rate))

    fake = _FakeRelay()
    gate = RelayAmbient(fake)
    check(gate.available, "中继执行端在 relay 正常时可用")
    check(gate.snapshot()["executor"] == "s3-relay", "执行端类型必须如实上报")
    gate.feed(b"closed", 16000)
    check(fake.fed == [], "关闭时音频包不得进入中继")
    check(gate.snapshot()["chunks_dropped"] == 1, "关闭时丢弃必须计数")
    check(gate.snapshot()["preroll_chunks"] == 1, "关闭时保留预缓冲包用于快速打开")

    gate.set_open(True)
    check(fake.fed == [(b"closed", 16000)],
          "打开瞬间先灌预缓冲（缩短可听延迟）")
    gate.feed(b"open", 16000)
    check(fake.fed == [(b"closed", 16000), (b"open", 16000)], "开放后音频包必须进入中继")
    snapshot = gate.snapshot()
    check(snapshot["open"] is True and snapshot["target"] is True, "开放状态必须进快照")
    check(snapshot["chunks_fed"] == 2, "放行包数必须计数（含预缓冲）")

    gate.set_open(False)
    gate.feed(b"again", 16000)
    check(fake.fed == [(b"closed", 16000), (b"open", 16000)], "关闭后不得再把包塞给中继")
    check(gate.snapshot()["open"] is False, "关闭状态必须进快照")

    fake.failure = "播放端报错"
    check(gate.available is False, "中继失败时执行端必须报不可用")
    check(gate.snapshot()["last_error"] == "播放端报错", "失败原因必须可见")

    state = HostState(source="serial")
    state.attach_ambient(gate)
    ambient_snapshot = state.snapshot()["ambient"]
    check(ambient_snapshot is not None and ambient_snapshot["executor"] == "s3-relay",
          "面板快照里要能看到中继执行端")


# --------------------------------------------------------------------------- 收尾修复


def test_capture_event_seq_is_in_snapshot() -> None:
    """capture 事件计数必须进快照：回控回执的判据就靠它。"""
    state = HostState(source="serial")
    eq(state.snapshot()["capture"]["event_seq"], 0, "还没事件时计数为 0")
    state.feed_capture(CaptureEvent(state="started", seconds=5, rate=16000))
    eq(state.snapshot()["capture"]["event_seq"], 1, "started 事件计数 +1")
    state.feed_capture(CaptureEvent(state="progress", progress={"chunks": 3}))
    state.feed_capture(CaptureEvent(state="finished", chunks=157))
    eq(state.snapshot()["capture"]["event_seq"], 3, "progress/finished 也都算新事件")


def test_control_ack_requires_a_new_capture_event() -> None:
    """回执必须有"发送之后的新事件"：粘性的 finished/stopped 不算。

    旧实现只看 capture_state in (started/stopped/finished)，于是第一次录音结束后，
    之后每条命令都会被立即回 ok:true——无论设备有没有收到。这里既测 ①（无新事件 →
    502/ack=false），也测 ②（发送后真来了新事件 → ack=true）。
    """
    import json as _json
    import threading
    import urllib.error
    import urllib.request

    from host.server import make_server

    state = HostState(source="serial")
    broadcaster = Broadcaster(state, min_interval=0.0)

    # 前置：已经有一段录音结束，粘性状态停在 finished（旧判据会把它当成已回应）。
    state.feed_capture(CaptureEvent(state="started", seconds=5, rate=16000))
    state.feed_capture(CaptureEvent(state="finished", chunks=157))
    eq(state.snapshot()["capture"]["state"], "finished", "前置：capture_state 停在 finished")

    writes: list[bytes] = []
    port = pick_port(18960)
    httpd = make_server(state, broadcaster, port, writer=lambda data: writes.append(data))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    result: dict = {}
    try:
        # ① 写成功，但没有任何新 capture 事件 → 必须如实报未获应答。
        request = urllib.request.Request(
            f"{base}/api/command",
            data=_json.dumps({"command": "rec stop"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            eq(exc.code, 502, "粘性 capture_state 不得被当成设备回应")
            result = _json.loads(exc.read())
        else:
            check(False, "粘性 capture_state 不得被当成设备回应")
        check(not result.get("ok") and result.get("acknowledged") is False,
              "没有新事件时必须明说未获应答")
        eq(writes, [b"rec stop\r\n"], "命令仍然要真的写进串口")

        # ② 设备在命令发出后真的产生了新事件 → acknowledged=true。
        def event_writer(data: bytes) -> None:
            writes.append(data)
            state.feed_capture(CaptureEvent(state="started", seconds=5, rate=16000))

        httpd.writer = event_writer
        request2 = urllib.request.Request(
            f"{base}/api/command",
            data=_json.dumps({"command": "rec start 5"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request2, timeout=10) as response:
            result2 = _json.loads(response.read())
        check(result2.get("ok") is True and result2.get("acknowledged") is True,
              "发送之后出现的新 capture 事件必须让回执成立")
        eq(state.snapshot()["capture"]["event_seq"], 3, "新事件继续累加")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_stop_cleanup_is_idempotent_and_runs_before_exit() -> None:
    """/api/stop 的退出路径必须先跑收尾，收尾本身幂等且动作互相隔离。"""
    import contextlib
    import io as _io

    from host.__main__ import make_cleanup
    from host.server import Handler

    calls: list[str] = []

    def step_a() -> None:
        calls.append("a")

    def step_b() -> None:
        calls.append("b")
        raise RuntimeError("模拟停止失败")

    def step_c() -> None:
        calls.append("c")

    cleanup = make_cleanup([("a", step_a), ("b", step_b), ("c", step_c)])
    cleanup()
    cleanup()  # 第二次必须是 no-op（两条退出路径可能各调一次）
    eq(calls, ["a", "b", "c"], "每个收尾动作只执行一次，一次失败不影响后面的动作")
    check(len(cleanup.errors) == 1 and "模拟停止失败" in cleanup.errors[0],
          "收尾失败必须被记录而不是抛出去")
    check(cleanup.done(), "cleanup 完成后要标记为已执行")

    # /api/stop 的退出体：先 on_stop()，再 exit_fn(0)；on_stop 抛异常也照样退出。
    exits: list[int] = []
    Handler._delayed_exit(delay=0, on_stop=cleanup, exit_fn=exits.append)
    eq(exits, [0], "on_stop 之后必须调用 exit_fn")

    calls.clear()
    with contextlib.redirect_stderr(_io.StringIO()):
        Handler._delayed_exit(
            delay=0,
            on_stop=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            exit_fn=exits.append,
        )
    eq(exits, [0, 0], "on_stop 抛异常也必须退出（异常被吞掉并记录）")
    check(calls == [], "第二次 cleanup 不得重复执行动作")


# --------------------------------------------------------------------------- 声纹判定


def test_voiceprint_decision_telemetry() -> None:
    """D28：设备侧 self-veto 判定的三个可选键要能解析、缺字段不猜默认值、快照带出来。"""
    base = {
        "type": "earbud_command",
        "schema": 4,
        "sequence": 1,
        "timestamp_ms": 100,
        "speech_gain_db": 0.0,
        "external_voice_gain_db": 0.0,
        "media_percent": 60,
        "state": "ADJUSTING",
        "environment": "VOICE",
        "mode": "FLAT",
        "conversation": "LIKELY",
        "confidence": 0.9,
        "execution": "decision_only",
        "link": "simulated",
    }
    with_decision = dict(
        base, voiceprint_valid=True, voiceprint_self=True, voiceprint_age_ms=42
    )
    frame = parse_line(json.dumps(with_decision)).value
    check(frame is not None and frame.voiceprint_valid is True, "voiceprint_valid 要解析进帧")
    check(frame is not None and frame.voiceprint_self is True, "voiceprint_self 要解析进帧")
    eq(frame.voiceprint_age_ms, 42, "voiceprint_age_ms 要解析进帧")

    old = parse_line(json.dumps(base)).value
    check(old is not None and old.voiceprint_valid is None,
          "旧固件不发三个键：必须是 None，不能猜成 False")
    check(old is not None and old.voiceprint_self is None and old.voiceprint_age_ms is None,
          "旧固件缺字段必须留空")

    partial = parse_line(json.dumps(dict(base, voiceprint_valid=True)))
    check(any("声纹判定字段只报了一部分" in p for p in partial.problems),
          "只报一部分必须记异常")
    negative_age = parse_line(
        json.dumps(dict(base, voiceprint_valid=False, voiceprint_self=False,
                        voiceprint_age_ms=-2))
    )
    check(any("声纹判定年龄" in p for p in negative_age.problems),
          "age_ms < -1 非法必须记异常")

    state = HostState(source="mock")
    state.feed_line(json.dumps(with_decision))
    decision = state.snapshot()["frame"]["voiceprint_decision"]
    eq(decision["valid"], True, "快照要带 voiceprint_decision.valid")
    eq(decision["self"], True, "快照要带 voiceprint_decision.self")
    eq(decision["age_ms"], 42, "快照要带 voiceprint_decision.age_ms")

    state2 = HostState(source="mock")
    state2.feed_line(json.dumps(base))
    decision2 = state2.snapshot()["frame"]["voiceprint_decision"]
    check(
        decision2["valid"] is None and decision2["self"] is None and decision2["age_ms"] is None,
        "没报时 voiceprint_decision 必须是三个 None，不能猜默认值",
    )


def test_assistant_snapshot_is_truthful_without_asr() -> None:
    service = AssistantService()
    snapshot = service.snapshot()
    check("state_text" in snapshot, "LLM 状态必须可显示")
    check("asr_ready" in snapshot and "asr_model" in snapshot,
          "ASR 加载状态和模型名必须进入上位机快照")
    eq(snapshot["transcript"], "尚无转写", "没接 ASR 时不能伪造转写")
    eq(snapshot["summary_state"], "尚无可总结会话", "没会话时不能伪造摘要")

    service.recording_started(16000, 5)
    during = service.snapshot()
    check("正在接收" in during["task"], "S3 开始录音必须成为 LLM 可见事件")
    check(during["capture_channel"]["active"], "页面必须明确显示采集通道已经开始")
    check(during["capture_channel"]["started_at"], "采集通道必须显示开始时间")
    service.recording_finished(audio_seconds=3.2, chunks=50, chunks_lost=0)
    finished = service.snapshot()
    eq(finished["today_sessions"], 1, "S3 完成录音必须计入今日会话")
    # 这次录音没有音频证据：后台派发线程必须尽快给出"未检测到有效人声"的真实状态，
    # 不能停在"正在处理"骗人（旧版会把静音送进 ASR，写出噪声文字）。
    check(
        wait_for(lambda: "未检测到有效人声" in service.snapshot()["transcript"]),
        "没有语音证据时必须如实显示跳过转写，不能停在处理中",
    )
    eq(finished["segments"][0]["end_reason"], "设备录音结束", "片段必须显示停止原因")

    stream = AssistantService()
    stream.recording_started(16000, 0)
    eq(stream.snapshot()["task"], "正在监听 S3 候选人声 · 16000 Hz",
       "无限录音流应被识别为传输通道，不能永远等待 finished")

    attributed = AssistantService(voiceprint_snapshot=lambda: {
        "ready": True,
        "history": [
            {"verdict": "self", "start_sample": 0, "end_sample": 12000},
            {"verdict": "other", "start_sample": 12000, "end_sample": 24000},
        ],
    })
    segments = attributed._speaker_segments(b"\0\0" * 24000, 16000, 0)
    eq([speaker for speaker, _ in segments], ["self", "other"],
       "声纹窗口必须切成带 self/other 身份的 ASR 轮次")
    unknown = AssistantService()._speaker_segments(b"\0\0" * 12000, 16000, 0)
    eq(unknown[0][0], "unknown", "缺失声纹证据时不得把转录归给任何人")

    # 2026-10-22 事故复盘：<0.45 s 的短轮次不再折叠身份；self 短应答是 voice 门的证据。
    short_self = AssistantService(voiceprint_snapshot=lambda: {
        "ready": True,
        "history": [
            {"verdict": "self", "start_sample": 0, "end_sample": 4800},
        ],
    })
    segments = short_self._speaker_segments(b"\0\0" * 4800, 16000, 0)
    eq([speaker for speaker, _ in segments], ["self"],
       "短于 0.45 s 的 self 轮次必须保留身份，不能折叠成 unknown")

    fallback = service._local_summary({
        "session": 3, "audio_seconds": 4.2, "chunks_lost": 0,
        "s3_ambient_open_intent_frames": 7, "transcript": "self：你好，这是一段测试。",
        "speaker_turns": [("self", "你好，这是一段测试。")],
    })
    check("ASR：self：你好，这是一段测试。" in fallback, "LLM 拒答时必须保留带身份逐字稿")
    check("仅检测到佩戴者" in fallback, "只有 self 时不得标成对话")
    check("不代表实际开放" in fallback, "本地摘要不得把 S3 意图写成执行事实")


def test_assistant_rolls_continuous_speech_into_asr_segments() -> None:
    class Frame:
        conversation = "LIKELY"
        environment = "VOICE"
        voice_likelihood = 0.9
        ambient_open = False

    service = AssistantService()
    dispatched = []
    service._dispatch_event = lambda *args: dispatched.append(args)  # type: ignore[method-assign]
    service.recording_started(16000, 0)
    service.observe_frame(Frame())
    service.feed_audio(type("Chunk", (), {"pcm": b"\0\0" * 1600, "rate": 16000})())
    service._conversation_started_at = time.monotonic() - service._MAX_CONTINUOUS_SEGMENT_S
    service.observe_frame(Frame())

    check(wait_for(lambda: len(dispatched) == 1),
          "连续人声达到分段上限后必须提交 ASR，不能无限等待 3 秒静音")
    check(dispatched[0][5], "滚动提交给 ASR 的片段必须包含实际音频")
    eq(service.snapshot()["today_sessions"], 1, "滚动提交也必须计入候选片段")
    segment = service.snapshot()["segments"][0]
    check(segment["started_at"] and segment["ended_at"], "片段必须显示明确的起止时间")
    eq(segment["end_reason"], "达到6秒，自动分段", "持续人声必须解释自动停止原因（6s 才能让判定赶上对话）")


def test_assistant_boosts_only_quiet_asr_copy() -> None:
    quiet = struct.pack("<100h", *([300] * 100))
    boosted, gain_db = AssistantService._prepare_asr_audio(quiet)
    check(gain_db > 17.9, "30厘米远场录音应获得受限的 ASR 副本增益")
    check(max(struct.unpack("<100h", boosted)) > 2000, "远场 PCM 必须实际被提升")

    loud = struct.pack("<100h", *([5000] * 100))
    unchanged, loud_gain = AssistantService._prepare_asr_audio(loud)
    eq(unchanged, loud, "足够响的录音不得再次放大")
    eq(loud_gain, 0.0, "足够响的录音应报告零 ASR 增益")


def test_ambient_requires_fresh_other_voiceprint() -> None:
    class Frame:
        def __init__(self, sequence, voice=0.9, event="speech"):
            self.sequence = sequence
            self.voice_likelihood = voice
            self.event = event

    check(voiceprint_allows_ambient({"ready": True, "verdict": "other", "age_s": 0.2}),
          "新鲜 other 才能放行环境声")
    for snapshot in (
        {"ready": True, "verdict": "self", "age_s": 0.2},
        {"ready": True, "verdict": None, "age_s": 0.2},
        {"ready": False, "verdict": "other", "age_s": 0.2},
        {"ready": True, "verdict": "other", "age_s": 3.1},
        {"ready": True, "verdict": "other", "age_s": None},
    ):
        check(not voiceprint_allows_ambient(snapshot),
              f"self/unknown/pending/stale/failure 必须关闭环境声：{snapshot}")

    gate = DialogueTurnGate(window_s=12.0)
    other = {"ready": True, "verdict": "other", "age_s": 0.2}
    own = {"ready": True, "verdict": "self", "age_s": 0.2}
    check(not gate.allows(other, Frame(1), now=1.0), "只有 other 不代表正在对话")
    check(not gate.allows(own, Frame(2), now=3.0), "自己说话时必须关闭环境声")
    check(not gate.allows(other, Frame(3), now=4.0), "一帧 other 人声不足以开放")
    check(not gate.allows(other, Frame(4), now=4.1), "两帧 other 人声不足以开放")
    check(gate.allows(other, Frame(5), now=4.2), "轮次成立且连续三帧对方人声才开放")
    check(not gate.allows(other, Frame(6, voice=0.2, event="keyboard"), now=4.3),
          "LIKELY 保持期里的键盘声必须立即关闭")
    check(not gate.allows(own, Frame(7), now=5.0), "对话中轮到自己时仍要关闭")
    check(not gate.allows(other, Frame(8), now=20.0), "轮次证据过期后不得沿用旧对话")


def test_assistant_filters_asr_noise_hallucinations() -> None:
    service = AssistantService()
    # 2026-10-21 现场截图：15 s 噪声被放大 +17 dB 后，Qwen3-ASR 吐出一整屏"就是"。
    screenshot = "当时我们是，" + "，".join(["就是"] * 60)
    cleaned, note = service._filter_asr_text(screenshot, 15.0)
    eq(cleaned, "", "满屏重复的 ASR 输出必须整段丢弃，不能把噪声写成文字")
    check("重复" in note, "丢弃原因必须可解释")

    for noisy in (
        "嗯。",                       # 只有语气词
        "嗯。嗯。嗯。",               # 语气词循环
        "听我儿子讲的。听我儿子讲的。听我儿子讲的。",  # 呼吸/环境音幻觉
        "这年。这年。这年。",
        "不会被patched，until tomorrow. 不会被patched，until tomorrow. 不会被patched，until tomorrow.",
    ):
        cleaned, _ = service._filter_asr_text(noisy, 15.0)
        eq(cleaned, "", f"噪声重复幻觉必须丢弃：{noisy}")

    kept, _ = service._filter_asr_text("你今晚几点到家？", 2.0)
    check("今晚" in kept, "正常转写不能被过滤")
    kept, _ = service._filter_asr_text("对对对，好的。", 2.0)
    check("好的" in kept, "正常口语重复不能整段丢弃")
    short_repeat, note = service._filter_asr_text("好的好的好的。", 1.2)
    check("好的" in short_repeat and note,
          "短句强调式重复应折叠保留，不能按噪声丢弃")
    for answer in ("好", "对。对。对。", "行", "收到"):
        kept, _ = service._filter_asr_text(answer, 1.5)
        check(bool(kept), f"真实短应答不能被当成噪声丢弃：{answer}")
    dropped, _ = service._filter_asr_text("嗯", 1.5)
    eq(dropped, "", "单字语气词仍应丢弃")
    # 2026-10-22 事故复盘：self 的短应答/语气词是"你接话了"的证据，必须保留。
    for answer in ("嗯", "嗯。", "啊", "喔"):
        kept, note = service._filter_asr_text(answer, 1.5, speaker="self")
        check(bool(kept) and "self保留" in note, f"self 短应答必须保留：{answer}")
    # other/unknown 的短应答仍按旧规则丢弃，防噪声幻觉污染 LLM。
    dropped_other, _ = service._filter_asr_text("嗯", 1.5, speaker="other")
    eq(dropped_other, "", "other 的单字语气词仍应丢弃")
    folded, note = service._filter_asr_text(
        "不会被patched，until tomorrow。", 3.0
    )
    check("patched" in folded, "单次正常句子必须保留")


def test_assistant_interaction_features() -> None:
    """对话交互结构必须显式给出，辅助 LLM 判断"是不是轮流开口"。"""
    service = AssistantService()
    features = service._interaction_features(
        [("self", "我讲一段"), ("other", "嗯"), ("self", "然后")], None
    )
    eq(features["self_turns"], 2, "本段 self 轮次数")
    eq(features["other_turns"], 1, "本段 other 轮次数")
    check(features["alternating"], "self→other→self 必须识别为交替")
    unknown = service._interaction_features(
        [("self", "我讲一段"), ("unknown", "x"), ("self", "继续")], None
    )
    check(not unknown["alternating"], "unknown 不参与交替判断（身份不明不能算轮次证据）")
    history = service._interaction_features(
        [("self", "好")],
        {"recent_turns": [{"speaker": "self"}, {"speaker": "other"}]},
    )
    check(history["recent_alternating"], "跨段 recent_turns 的交替必须识别")


def test_assistant_gates_asr_on_speech_evidence() -> None:
    rate = 16000
    silence = struct.pack("<16000h", *([0] * 16000))
    regions, seconds, note = AssistantService._select_speech_regions(silence, rate, [], 0)
    eq(regions, [], "纯静音不得产生语音区间")
    check(seconds < AssistantService._MIN_SPEECH_S, "纯静音的证据时长必须低于 ASR 门槛")

    # 0.5 s 静音 + 1 s 近场能量 + 0.2 s 静音：能量 VAD 必须找到中间那一段。
    tone = [8000 if index % 2 else -8000 for index in range(rate)]
    samples = [0] * int(0.5 * rate) + tone + [0] * int(0.2 * rate)
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    regions, seconds, note = AssistantService._select_speech_regions(pcm, rate, [], 0)
    check(seconds >= AssistantService._MIN_SPEECH_S, "近场能量必须能独立放行 ASR")
    trimmed, start = AssistantService._trim_to_speech(pcm, rate, regions)
    check(len(trimmed) < len(pcm), "ASR 副本必须裁掉首尾静音")
    check(start > 0, "裁剪后必须保留原始样本偏移，声纹窗口才对得上")
    check(len(trimmed) >= int(0.8 * rate) * 2, "裁剪不能把有效语音裁掉")

    # 设备遥测的连续语音证据也要能放行（音频本身是静音，模拟串口帧先行）。
    telemetry = [
        {"voice_likelihood": 0.9, "event": "speech", "sample": int(rate * 0.05 * index)}
        for index in range(30)
    ]
    long_silence = struct.pack("<32000h", *([0] * 32000))
    _, device_seconds, _ = AssistantService._select_speech_regions(long_silence, rate, telemetry, 0)
    check(device_seconds >= AssistantService._MIN_SPEECH_S, "设备连续语音证据必须能独立放行 ASR")

    # 连续 4 帧也不能靠键盘/风事件混过语音证据门。
    for event in ("keyboard", "wind"):
        rejected = [
            {"voice_likelihood": 0.95, "event": event, "sample": int(rate * 0.2 * index)}
            for index in range(6)
        ]
        eq(AssistantService._telemetry_speech_regions(rejected, rate, 0, 48000), [],
           f"连续 {event} 帧不得成为 ASR 语音证据")
    control = [
        {"voice_likelihood": 0.95, "event": "speech", "sample": int(rate * 0.2 * index)}
        for index in range(6)
    ]
    check(_intervals_seconds(
              AssistantService._telemetry_speech_regions(control, rate, 0, 48000), rate
          ) > 0,
          "连续 speech 帧必须能通过语音证据门（对照组）")


def test_assistant_fast_path_authorization_and_late_veto() -> None:
    """D34：端侧快路径默认放行；LLM 只有迟到的语义否决权。"""
    service = AssistantService()
    service.base_url = "http://127.0.0.1:9"
    service.api_key = "test"
    service.model = "test-model"

    waiting = service.ambient_authorization()
    check(waiting["allow"] and waiting["state"] == "fast_local",
          "没有 LLM 判定时不能阻塞端侧快路径")
    check(not waiting["veto"], "没有判定不能凭空否决")

    service._publish_decision(1, "yes", "open", 0.85, "双方来回", "llm")
    confirmed = service.ambient_authorization()
    check(confirmed["allow"] and confirmed["state"] == "llm_confirmed",
          "LLM yes 必须确认为对话（不阻塞快路径）")
    check(confirmed["age_s"] is not None and confirmed["age_s"] <= 1.0,
          "授权必须带判定年龄")

    service._decision["at"] = time.monotonic() - service._DECISION_TTL_S - 1.0
    stale = service.ambient_authorization()
    check(stale["allow"] and stale["state"] == "fast_local",
          "过期 yes 只能被忽略，不能阻塞快路径；也不应冒充新鲜确认")

    # LLM 迟到否决：即使设备意图为 true，执行端也要关闭。
    service._publish_decision(2, "no", "close", 0.8, "电视单向人声", "llm")
    veto = service.ambient_authorization()
    check((not veto["allow"]) and veto["state"] == "llm_veto" and veto["veto"],
          "LLM 判定非对话必须形成迟到否决")
    check(veto["age_s"] is not None and veto["age_s"] <= 1.0,
          "否决也必须带判定年龄，便于计算误开时长")

    # 本地 unclear 不参与语义否决，快路径继续；本地 no（无有效语音）要否决，
    # 否则纯噪声里环境声会一直开着（ASR 过滤空时不会调用 LLM 来纠正）。
    service._publish_decision(3, "unclear", "close", 0.2, "ASR 只输出语气词", "local")
    local = service.ambient_authorization()
    check(local["allow"] and local["state"] == "fast_local",
          "本地 unclear 判定不能否决端侧快路径")
    service._publish_decision(4, "no", "close", 0.9, "无有效语音", "local")
    local_veto = service.ambient_authorization()
    check((not local_veto["allow"]) and local_veto["state"] == "local_veto",
          "本地无有效语音必须能关闭快路径已经打开的环境声")

    # 旧轮次判定晚归：不能写入新轮次，快路径继续。
    delayed = AssistantService()
    delayed.base_url = "http://127.0.0.1:9"
    delayed.api_key = "test"
    delayed.model = "test-model"
    delayed.recording_started(16000, 0)
    old_epoch = delayed._authorization_epoch_id - 1
    delayed._publish_decision(
        5, "no", "close", 0.9, "上一轮否决晚归", "llm", epoch_id=old_epoch
    )
    check(delayed.snapshot()["decision"] is None,
          "上一轮对话的判定晚归必须被丢弃")
    check(delayed.ambient_authorization()["allow"],
          "旧轮次的否决不得关掉新轮次的快路径")

    # 新一次定时录音必须清掉上一段录音的判定。
    timed = AssistantService()
    timed.base_url = "http://127.0.0.1:9"
    timed.api_key = "test"
    timed.model = "test-model"
    timed.recording_started(16000, 5)
    timed._publish_decision(1, "no", "close", 0.9, "上一段录音", "llm")
    check(timed.ambient_authorization()["state"] == "llm_veto", "前置条件：否决已生效")
    timed.recording_started(16000, 5)
    check(timed.snapshot()["decision"] is None, "新一次录音必须清掉上一次录音的判定")
    check(timed.ambient_authorization()["allow"], "新录音从端侧快路径开始")

    parsed = AssistantService._parse_decision(
        '{"dialogue":"yes","ambient":"open","confidence":0.8,"reason":"双方来回"}'
    )
    eq(parsed["dialogue"], "yes", "结构化 LLM 判定必须可解析")
    eq(parsed["ambient"], "open", "结构化 LLM 判定必须保留环境声意图")
    eq(AssistantService._parse_decision("对不起，我需要更多信息"), None,
       "非 JSON 输出必须触发本地回退")
    contradictory = AssistantService._parse_decision(
        '{"dialogue":"no","ambient":"open","confidence":1.0,"reason":"单方"}'
    )
    eq(contradictory["ambient"], "close", "非对话判定不得携带开放环境声")

    snapshot = service.snapshot()
    check(snapshot["decision"] is not None, "最近判定必须进入快照供界面显示")
    eq(snapshot["ambient_authorization"]["state"], "local_veto",
       "快照必须带当前环境声授权状态（本地无语音否决）")
    eq(AssistantService._asr_token_budget(6.0), 72, "6 s 语音的 ASR token 预算必须按 12 token/s 收紧")
    eq(AssistantService._asr_token_budget(30.0), AssistantService._MAX_ASR_TOKENS,
       "超长语音的 ASR token 预算必须封顶")


def test_authorization_epoch_advances_after_rolling_cap_then_silence() -> None:
    """6 s 上限结束后即使会话处于 inactive，静音后再开口也必须算新一轮对话。"""
    class Frame:
        conversation = "LIKELY"
        environment = "VOICE"
        ambient_open = False
        voice_likelihood = 0.9
        event = "speech"

    service = AssistantService()
    service.base_url = "http://127.0.0.1:9"
    service.api_key = "test"
    service.model = "test-model"
    service.recording_started(16000, 0)
    service._spawn_dispatch = lambda *args: None  # type: ignore[method-assign]
    service.observe_frame(Frame())
    first_epoch = service._authorization_epoch_id
    service._publish_decision(1, "no", "close", 0.9, "第一轮是电视", "llm")
    check(service.ambient_authorization()["state"] == "llm_veto",
          "第一轮对话内的否决必须生效")

    # 达到 6 s 上限：会话转为 inactive，但轮次不变（滚动分段）。
    service._conversation_started_at = time.monotonic() - service._MAX_CONTINUOUS_SEGMENT_S
    service.observe_frame(Frame())
    eq(service._authorization_epoch_id, first_epoch,
       "达到 6 s 上限的滚动分段不得推进授权轮次")
    service.observe_frame(type("Silent", (), {"conversation": "NONE"})())
    eq(service._authorization_epoch_id, first_epoch,
       "滚动分段结束后的静音帧不得推进授权轮次")

    # 静音 4 s 后重新开口：推进轮次，上一轮的语义否决不应影响新对话。
    service._last_conversation_at = time.monotonic() - 4.0
    service.observe_frame(Frame())
    eq(service._authorization_epoch_id, first_epoch + 1,
       "静音后重新开口必须推进授权轮次")
    check(service.ambient_authorization()["allow"],
          "新一轮对话必须从端侧快路径开始，不继承上一轮否决")


def test_assistant_asr_death_falls_back_to_fast_path() -> None:
    # 并发 LLM 请求乱序返回：旧摘要不能覆盖新摘要。
    service = AssistantService()
    service._set_summary(3, "第三段摘要")
    service._set_summary(2, "第二段旧摘要")
    eq(service.snapshot()["summary_state"], "第三段摘要",
       "旧片段摘要不得覆盖新片段摘要")

    # ASR worker 的 stdout 关闭（崩溃/被杀）：退回纯本地快路径，不能永久卡住。
    class FakeProcess:
        stdout = iter(())

    published = AssistantService()
    published.base_url = "http://127.0.0.1:9"
    published.api_key = "test"
    published.model = "test-model"
    published._publish_decision(2, "yes", "open", 0.9, "刚判为对话", "llm")
    published._asr_ready = True
    published._asr_process = FakeProcess()  # type: ignore[assignment]
    published._read_asr()
    snapshot = published.snapshot()
    check(not snapshot["asr_ready"], "ASR worker 退出后必须标记为未就绪")
    check("已退出" in snapshot["task"], "ASR worker 退出必须反映在任务状态里")
    check(snapshot["ambient_authorization"]["allow"],
          "ASR 退出只能降级为无 LLM 否决，不能阻塞端侧快路径")


def test_assistant_aggregates_speaker_labels_per_turn() -> None:
    """说话人归属按"声学轮次"聚合，不再被 0.75 s 窗口切碎（D45）。

    旧逻辑：0.75 s 切片逐片贴标签，用户一句 1–2 s 的自说会切成 self+unknown 两段。
    新逻辑：能量 VAD 切轮次，每轮用中点落在轮内的声纹窗投票；self 优先，短轮不贴身份。
    这里用合成 PCM + 假声纹窗，锁住三件事：轮次完整、self 不被 unknown 切片切碎、
    没有声纹证据时仍回 unknown。
    """
    import array

    rate = 16000
    total = rate * 3  # 3 s
    samples = array.array("h", [200 if index % 2 == 0 else -200 for index in range(total)])
    # 1.0–2.5 s 是一段"说话"（+32 dB 高于本底）
    for index in range(rate, int(rate * 2.5)):
        samples[index] = 8000 if index % 2 == 0 else -8000
    audio = samples.tobytes()

    window = int(rate * 1.5)
    # self 窗中点 1.3 s，落在整段说话轮内；没有别的窗覆盖它。
    history = [{
        "verdict": "self", "score": 0.93, "dbfs": -15.0, "speech_ratio": 1.0,
        "start_sample": int(rate * 0.5), "end_sample": int(rate * 0.5) + window,
    }]
    service = AssistantService(voiceprint_snapshot=lambda: {"ready": True, "history": history})
    segments = service._speaker_segments(audio, rate, 0)  # type: ignore[attr-defined]
    labels = [speaker for speaker, _ in segments]
    check(labels.count("self") == 1, f"整段自说应聚合成一个 self 轮次，实际 {labels}")
    check("unknown" not in labels, f"同一句不能被 unknown 切片切碎，实际 {labels}")
    self_pcm = next(pcm for speaker, pcm in segments if speaker == "self")
    check(len(self_pcm) >= int(rate * 0.45) * 2, "聚合后的 self 轮次要包含完整语音")

    # 只有 unknown 窗（没有 self/other 证据）时，整轮仍是 unknown，不能猜。
    only_unknown = [{
        "verdict": "unknown", "score": 0.70, "dbfs": -20.0, "speech_ratio": 1.0,
        "start_sample": int(rate * 0.5), "end_sample": int(rate * 0.5) + window,
    }]
    service2 = AssistantService(
        voiceprint_snapshot=lambda: {"ready": True, "history": only_unknown}
    )
    segments2 = service2._speaker_segments(audio, rate, 0)  # type: ignore[attr-defined]
    check([speaker for speaker, _ in segments2] == ["unknown"], "无身份证据时必须 unknown")


def test_assistant_splits_unknown_speakers_into_pause_turns() -> None:
    """声纹全 unknown 时，按静音停顿切轮次，LLM 才能看到"轮次结构"。"""
    rate = 16000
    tone = [8000 if index % 2 else -8000 for index in range(int(0.6 * rate))]
    silence = [0] * int(0.5 * rate)
    samples = silence + tone + silence + tone + [0] * int(0.2 * rate)
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    first = len(silence)
    second = first + len(tone) + len(silence)
    regions = [
        (first, first + len(tone)),
        (second, second + len(tone)),
    ]
    turns = AssistantService._split_unknown_turns(pcm, rate, regions, 0)
    eq(len(turns), 2, "两个静音分开的语音区间必须切成两个 unknown 轮次")
    check(all(label == "unknown" for label, _ in turns),
          "切轮次不能伪造说话人身份")
    check(all(len(audio) >= int(0.3 * rate) * 2 for _, audio in turns),
          "每个轮次都必须保留足够 ASR 音频")


def test_training_writer_records_segment_and_llm_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        training = TrainingWriter(tmp, audio=True)
        service = AssistantService(training=training)
        service.recording_started(16000, 5)
        # 1 s 静音：没有语音证据，走跳过 ASR 分支；训练记录仍应落盘（负样本）。
        service._dispatch_event(
            1, 1.0, 1, 0, [], struct.pack("<16000h", *([0] * 16000)), 16000, 0
        )
        lines = (training.session_dir / "segments.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        eq(len(lines), 1, "每个候选片段必须写一条训练记录（含负样本）")
        record = json.loads(lines[0])
        eq(record["segment"], 1, "训练记录必须带片段号")
        check(record["audio"] and record["audio"]["path"], "默认必须保存原始音频")
        check((training.session_dir / record["audio"]["path"]).exists(),
              "音频文件必须真的落盘")
        eq(record["device"]["speech_s"], 0.0, "负样本要记录端侧语音证据为 0")
        eq(record["labels"]["teacher"]["dialogue"], "no",
           "跳过 ASR 的片段教师标签应是本地非对话")
        check("llm_context" in record, "训练记录必须保存 LLM 上下文快照")
        eq(record["labels"]["human"], None, "人工标签留空，等后续标注")
        service.recording_started(16000, 5)
        json.dumps(service.snapshot(), ensure_ascii=False)  # 快照必须可 JSON 序列化
        training.close()

    # LLM 上下文：同一轮对话内累积轮次与判定；新采集/新轮次清空。
    service = AssistantService()
    service.recording_started(16000, 0)
    epoch = service._authorization_epoch_id
    service._note_context_turns(1, epoch, [("self", "你几点到？"), ("other", "七点。")])
    service._note_context_turns(1, epoch, [("self", ""), ("other", "")])
    facts = service._context_facts()
    # self 空文本也要留轮次证据（other/unknown 空文本跳过）。
    check(any(t["speaker"] == "self" and t["text"] == "（短应答，内容未识别）"
              for t in facts["recent_turns"]),
          "self 内容被过滤时也要留占位轮次，不能让 LLM 判成'无 self 回应'")
    service.recording_started(16000, 0)  # 新会话会推进 epoch
    epoch = service._authorization_epoch_id
    service._note_context_turns(2, epoch, [("unknown", "第一句"), ("unknown", "第二句")])
    facts = service._context_facts()
    eq(facts["turn_count"], 2, "LLM 上下文必须保存跨片段轮次")
    service._publish_decision(2, "yes", "open", 0.8, "轮流接话", "llm")
    check(len(service._context_facts()["previous_verdicts"]) == 1,
          "LLM 上下文必须保存最近判定，供下一段保持一致")
    service.recording_started(16000, 0)
    eq(service._context_facts()["turn_count"], 0, "新一轮采集必须清空 LLM 上下文")


def test_assistant_uses_absolute_sample_offset_for_speakers() -> None:
    """裁剪后的 ASR 片段仍要按**绝对**流样本号对齐声纹窗口（连续中继 start_sample>0）。"""
    absolute_start = 100_000
    # 裁剪后的片段绝对起点 108000，长 1.5 s；声纹窗口前 0.75 s self、后 0.75 s other。
    trimmed_start = absolute_start + 8000
    voiceprint = AssistantService(voiceprint_snapshot=lambda: {
        "ready": True,
        "history": [
            {"verdict": "self", "start_sample": trimmed_start,
             "end_sample": trimmed_start + 12000},
            {"verdict": "other", "start_sample": trimmed_start + 12000,
             "end_sample": trimmed_start + 24000},
        ],
    })
    pcm = b"\0\0" * 24000
    segments = voiceprint._speaker_segments(pcm, 16000, trimmed_start)
    eq([speaker for speaker, _ in segments], ["self", "other"],
       "裁剪后必须用绝对样本偏移切分 self/other，而不是相对偏移")

    # 调用点回归：_dispatch_event 传给 _speaker_segments 的必须是 start_sample+trim_start。
    captured: dict[str, int] = {}

    class FakeStdin:
        def write(self, data: str) -> None:
            pass

        def flush(self) -> None:
            pass

    class FakeProcess:
        stdin = FakeStdin()

    service = AssistantService()
    service._asr_ready = True
    service._asr_process = FakeProcess()  # type: ignore[assignment]
    service._select_speech_regions = lambda *args: ([(16000, 32000)], 1.0, "test")  # type: ignore[method-assign]
    service._trim_to_speech = lambda pcm, rate, regions: (pcm, 1234)  # type: ignore[method-assign]

    def capture_segments(audio, rate, start):
        captured["start"] = int(start)
        return [("unknown", audio)]

    service._speaker_segments = capture_segments  # type: ignore[method-assign]
    service._dispatch_event(7, 1.0, 1, 0, [], b"\0\0" * 32000, 16000, 100_000)
    eq(captured.get("start"), 100_000 + 1234,
       "ASR 轮次必须按绝对样本号切分（start_sample + trim_start）")


def test_llm_ambient_authority_helpers() -> None:
    class NoLlm:
        configured = False

        @staticmethod
        def ambient_authorization() -> dict:
            return {"allow": False, "state": "closed", "reason": "不应被采用"}

    check(llm_ambient_authorization(NoLlm())["allow"],
          "LLM 未配置时必须回退到设备意图/声纹门控")
    check(llm_ambient_authorization(None)["allow"], "没有 assistant 时不得凭空关闭环境声")

    class Waiting:
        configured = True

        @staticmethod
        def ambient_authorization() -> dict:
            return {"allow": False, "state": "waiting", "reason": "尚无判定"}

    check(not llm_ambient_authorization(Waiting())["allow"],
          "LLM 已配置但没有判定时必须 fail-closed")

    class Broken:
        configured = True

    check(not llm_ambient_authorization(Broken())["allow"],
          "授权接口缺失时必须 fail-closed")

    check(fresh_self_veto({"ready": True, "verdict": "self", "age_s": 0.5}),
          "新鲜 self 必须能否决环境声")
    for snapshot in (
        {"ready": True, "verdict": "other", "age_s": 0.5},
        {"ready": True, "verdict": "self", "age_s": 3.2},
        {"ready": False, "verdict": "self", "age_s": 0.1},
        {"ready": True, "verdict": "self", "age_s": None},
    ):
        check(not fresh_self_veto(snapshot),
              f"other/过期/未就绪都不得触发 self 否决：{snapshot}")


# --------------------------------------------------------------------------- 连续音频


def _pcm_chunk(
    sequence: int | None,
    *,
    samples: int = 320,
    start: int = 0,
    rate: int = 16_000,
) -> MicChunk:
    """造一块与固件同形的 16-bit PCM（值域避开 ±32767，便于逐字节比对）。"""
    data = struct.pack(
        f"<{samples}h", *[((start + i) % 20_000) - 10_000 for i in range(samples)]
    )
    return MicChunk(
        sequence=sequence,
        sample_count=samples,
        rate=rate,
        timestamp_ms=None if sequence is None else sequence * 20,
        pcm=data,
    )


def test_continuous_audio_writes_one_contiguous_wav() -> None:
    """连续音频必须"整段连续"：样本顺序逐字节一致、WAV 头合法、summary 与统计对得上。"""
    with tempfile.TemporaryDirectory() as folder:
        writer = ContinuousAudioWriter(folder, chunk_seconds=1.0)
        chunks = [_pcm_chunk(seq, start=seq * 320) for seq in range(5)]
        for chunk in chunks:
            check(writer.feed(chunk), "连续音频每块都应写入成功")
        stats = writer.stats()
        eq(stats["chunks"], 5, "块数")
        eq(stats["gaps"], 0, "序号连续时不得记丢块")
        eq(stats["lost_chunks"], 0, "序号连续时丢块数为 0")
        eq(stats["duration_s"], 0.1, "时长（1600 点 @ 16 kHz）")
        eq(stats["error"], "", "正常写入不得留下错误")
        writer.close()
        writer.close()  # 两条退出路径会各调一次，必须幂等

        session = Path(stats["session_dir"])
        meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
        eq(meta["rate"], 16_000, "会话元信息采样率")
        summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
        eq(summary["end_reason"], "stopped", "正常退出原因")
        eq(summary["chunks"], 5, "summary 块数")
        eq(summary["files"], ["audio-0001.wav"], "未到轮转阈值只应有一个文件")

        with wave.open(str(session / "audio-0001.wav"), "rb") as handle:
            eq(handle.getframerate(), 16_000, "WAV 采样率")
            eq(handle.getnchannels(), 1, "WAV 单声道")
            eq(handle.getsampwidth(), 2, "WAV 16-bit")
            eq(handle.getnframes(), 1_600, "WAV 样本数")
            check(
                handle.readframes(handle.getnframes()) == b"".join(c.pcm for c in chunks),
                "WAV 样本必须与写入顺序逐字节一致（收尾不得丢样本）",
            )

        lines = (session / "capture.jsonl").read_text(encoding="utf-8").splitlines()
        eq(len(lines), 5, "时间轴每块一行")
        first = json.loads(lines[0])
        eq(first["file"], "audio-0001.wav", "时间轴文件名")
        eq(first["offset_s"], 0.0, "首块在文件内的偏移")
        eq(json.loads(lines[1])["offset_s"], 0.02, "第二块偏移 = 320/16000")


def test_continuous_audio_rotation_keeps_samples_contiguous() -> None:
    """轮转是文件边界，不是样本边界：拆分后拼接必须与原始顺序逐字节一致。"""
    with tempfile.TemporaryDirectory() as folder:
        writer = ContinuousAudioWriter(folder, chunk_seconds=0.01)  # 160 点/文件
        chunks = [_pcm_chunk(seq, samples=100, start=seq * 100) for seq in range(4)]
        for chunk in chunks:
            check(writer.feed(chunk), "轮转场景下每块都应写入成功")
        stats = writer.stats()
        eq(
            stats["files"],
            ["audio-0001.wav", "audio-0002.wav"],
            "100 点/块、160 点阈值：第 3 块前轮转，只应有两个文件",
        )
        writer.close()
        session = Path(stats["session_dir"])
        joined = b""
        sizes: list[int] = []
        for name in stats["files"]:
            with wave.open(str(session / name), "rb") as handle:
                sizes.append(handle.getnframes())
                joined += handle.readframes(handle.getnframes())
        eq(sizes, [200, 200], "每个文件的实际样本数")
        check(joined == b"".join(c.pcm for c in chunks), "轮转不得丢样本或错序")


def test_continuous_audio_gaps_and_rate_mismatch_are_explicit() -> None:
    """丢块只记账不补零；采样率不一致拒绝写入——两种失败都不能被伪装成正常音频。"""
    with tempfile.TemporaryDirectory() as folder:
        writer = ContinuousAudioWriter(folder)
        check(writer.feed(_pcm_chunk(0)), "首块写入")
        check(writer.feed(_pcm_chunk(5)), "跳号也照写（录音不能因丢块中断）")
        stats = writer.stats()
        eq(stats["gaps"], 1, "跳号必须记一处丢块")
        eq(stats["lost_chunks"], 4, "0→5 之间丢 4 块")
        check(not writer.feed(_pcm_chunk(6, rate=8_000)), "采样率不一致必须拒绝写入")
        check("采样率" in writer.error, f"必须给出可读原因（实得 {writer.error!r}）")
        check(not writer.feed(_pcm_chunk(7)), "拒绝之后不得继续写入（不许污染 WAV）")
        writer.close()

        session = Path(stats["session_dir"])
        lines = (session / "capture.jsonl").read_text(encoding="utf-8").splitlines()
        eq(len(lines), 2, "被拒绝的块不得进时间轴")
        check(json.loads(lines[1])["gap"] is True, "跳号行必须显式标 gap")
        summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
        eq(summary["gaps"], 1, "summary 里的丢块数")
        check("采样率" in summary["error"], "summary 必须保留拒绝原因")
        with wave.open(str(session / "audio-0001.wav"), "rb") as handle:
            eq(handle.getnframes(), 640, "只写进了被接受的两块（不补零）")


def test_continuous_audio_stops_at_disk_budget() -> None:
    """磁盘预算到顶必须停写并留下原因，不能悄悄把演示机磁盘写满。"""
    with tempfile.TemporaryDirectory() as folder:
        writer = ContinuousAudioWriter(folder, max_bytes=2_000)
        written = 0
        while written < 10 and writer.feed(_pcm_chunk(written)):
            written += 1
        eq(written, 4, "640 B/块，写到第 4 块（2560 B）越 2000 B 预算即停")
        check(writer.stats()["stopped_by_budget"], "必须标记磁盘预算止损")
        check(not writer.feed(_pcm_chunk(99)), "止损后不得再写入")
        writer.close()
        writer.close()
        session = Path(writer.stats()["session_dir"])
        summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
        eq(summary["end_reason"], "stopped_by_budget", "结束原因")
        eq(summary["chunks"], 4, "已写入块数")
        with wave.open(str(session / "audio-0001.wav"), "rb") as handle:
            eq(handle.getnframes(), 4 * 320, "预算止损前的样本已正确收尾")


def test_continuous_audio_cli_defaults() -> None:
    """默认关闭：连续音频约 150 MB/小时，不能因为升级 host 就悄悄开始落盘。"""
    args = parse_args([])
    eq(args.continuous_audio, "", "默认不写连续音频")
    eq(args.continuous_chunk_seconds, 600.0, "默认轮转 600 s")
    args = parse_args(
        ["--continuous-audio", "记录/连续音频", "--continuous-chunk-seconds", "60"]
    )
    eq(args.continuous_audio, "记录/连续音频", "显式打开时保留目录")
    eq(args.continuous_chunk_seconds, 60.0, "轮转长度可配")


def test_main_wires_continuous_audio_from_replay() -> None:
    """main() 装配连续音频：回放里的 mic_chunk 要落成 WAV，退出收尾要写 summary。

    这一组挡的是装配顺序问题（例如 continuous 定义在 on_mic_chunk 之后、cleanup 漏挂）
    ——单测看不见，真机表现为启动即崩或"跑了一小时没有文件"。
    回放源结束会让 main() 走正常退出路径（与 Ctrl+C 共用同一个 cleanup），所以不需要
    /api/stop，也不会动到真进程。
    """
    import threading

    import host.__main__ as main_module
    from host.__main__ import main
    from host.server import pick_port

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        audio_dir = root / "连续音频"
        chunks: list[bytes] = []
        lines: list[str] = []
        for seq in range(8):
            chunk = _pcm_chunk(seq)
            chunks.append(chunk.pcm)
            lines.append(
                json.dumps(
                    {
                        "type": "mic_chunk",
                        "sequence": seq,
                        "timestamp_ms": seq * 20,
                        "samples": 320,
                        "rate": 16_000,
                        "pcm": base64.b64encode(chunk.pcm).decode(),
                    }
                )
            )
        log = root / "mix.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # main() 会装 SIGINT/SIGTERM 处理器，而 signal.signal 只能在主线程调用。
        original_signal = main_module.signal.signal
        main_module.signal.signal = lambda *args, **kwargs: None  # noqa: ARG005
        port = pick_port(18970)
        result: dict[str, int] = {}

        def run() -> None:
            result["rc"] = main(
                [
                    "--replay",
                    str(log),
                    "--quiet",
                    "--no-browser",
                    "--no-player",
                    "--no-memo",
                    "--continuous-audio",
                    str(audio_dir),
                    "--web-port",
                    str(port),
                ]
            )

        thread = threading.Thread(target=run, daemon=True)
        try:
            thread.start()
            thread.join(timeout=15)
            check(not thread.is_alive(), "回放源结束后 main() 必须正常退出")
            eq(result.get("rc"), 0, "main() 退出码")
            summaries = sorted(audio_dir.glob("*/summary.json"))
            eq(len(summaries), 1, "一个会话目录一个 summary")
            if not summaries:
                return
            summary = json.loads(summaries[0].read_text(encoding="utf-8"))
            eq(summary["chunks"], 8, "回放的 8 个 mic_chunk 都要记账")
            eq(summary["gaps"], 0, "序号连续不得记丢块")
            eq(summary["end_reason"], "stopped", "退出路径要留下原因")
            eq(summary["error"], "", "正常退出不得留错误")
            wavs = sorted(summaries[0].parent.glob("audio-*.wav"))
            eq(len(wavs), 1, "未到轮转阈值只应有一个 WAV")
            if wavs:
                with wave.open(str(wavs[0]), "rb") as handle:
                    eq(handle.getframerate(), 16_000, "连续 WAV 采样率")
                    eq(handle.getnframes(), 8 * 320, "连续 WAV 样本数")
                    check(
                        handle.readframes(handle.getnframes()) == b"".join(chunks),
                        "样本顺序必须与回放顺序一致",
                    )
        finally:
            main_module.signal.signal = original_signal
            thread.join(timeout=3)


def test_execution_pause_endpoint_and_snapshot() -> None:
    """POST /api/execution 必须真的改变快照里的状态（不是只回 ok）。

    这是 2026-10-26 新增的面板暂停/继续入口：面板按钮读 snapshot.execution_paused，
    端点只负责置位；执行端的复位由主循环在暂停沿完成（见下一组测试）。
    """
    import threading
    import urllib.request

    from host.server import make_server, pick_port

    state = HostState(source="mock")
    broadcaster = Broadcaster(state, min_interval=0.0)
    port = pick_port(18960)
    httpd = make_server(state, broadcaster, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def post_paused(value: bool) -> dict:
        request = urllib.request.Request(
            f"{base}/api/execution",
            data=json.dumps({"paused": value}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        check(state.snapshot()["execution_paused"] is False, "默认不是暂停状态")
        payload = post_paused(True)
        check(payload.get("ok") is True and payload.get("paused") is True,
              "暂停请求返回 ok + paused")
        check(state.snapshot()["execution_paused"] is True, "暂停后快照反映已暂停")
        payload = post_paused(False)
        check(payload.get("ok") is True and payload.get("paused") is False,
              "继续请求返回 ok + paused")
        check(state.snapshot()["execution_paused"] is False, "继续后快照回到运行中")
        bad = urllib.request.Request(
            f"{base}/api/execution",
            data=b'{"paused": "yes"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(bad, timeout=5)
            check(False, "非布尔 paused 必须被拒绝")
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, "非布尔 paused 返回 400")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pause_resets_execution_side() -> None:
    """暂停必须把执行端复位（平直 / 媒体 100%），不能把上一帧的 EQ 留在音频上。"""
    from host.__main__ import apply_neutral_execution

    class FakePlayer:
        def __init__(self) -> None:
            self.calls: list[tuple[list[float], float]] = []

        def apply_decision(self, bands, media_percent):
            self.calls.append((list(bands), media_percent))
            return True

    class FakeSystemEq(FakePlayer):
        def apply_decision(self, bands, media_percent, depth_override=None, base_gain=1.0):
            self.calls.append((list(bands), media_percent, depth_override, base_gain))
            return True

    player = FakePlayer()
    check(apply_neutral_execution(player, None) is True, "播放器执行端复位成功")
    eq(len(player.calls), 1, "复位只下发一次")
    bands, media = player.calls[0]
    check(len(bands) == 24 and all(v == 0.0 for v in bands), "复位曲线全 0（平直）")
    eq(media, 100.0, "复位媒体 100%")

    systemeq = FakeSystemEq()
    check(apply_neutral_execution(None, systemeq) is True, "systemeq 执行端复位成功")
    bands, media, _depth, base_gain = systemeq.calls[0]
    check(all(v == 0.0 for v in bands) and media == 100.0,
          "systemeq 复位 = 平直 + 媒体 100%")
    eq(base_gain, 0.0, "systemeq 基线归零，不留 demo 音染")
    check(apply_neutral_execution(None, None) is False, "没有执行端时返回 False，不伪报成功")


def main() -> int:
    tests = [
        test_d2_matches_c_printf,
        test_mac_default_output_route,
        test_earphone_cli_parsing,
        test_memo_persists_injects_and_finalizes,
        test_memo_retention_and_decay,
        test_command_frame_full,
        test_new_firmware_frame_carries_tuning,
        test_context_model_frame,
        test_context_mixed_fields,
        test_dual_mic_telemetry,
        test_event_frame,
        test_speech_gate_frame,
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
        test_execution_pause_endpoint_and_snapshot,
        test_pause_resets_execution_side,
        test_interval_stats_and_percentile,
        test_player_decision_path_is_not_silently_dropped,
        test_eq_mode_and_bypass_control,
        test_voiceprint_shadow_snapshot_is_harmless,
        test_voiceprint_energy_activity_fallback,
        test_ambient_executor_and_intent_parsing,
        test_ambient_gate_dual_turn_trigger,
        test_ambient_gate_allows,
        test_relay_ambient_gate,
        test_audio_relay_budget_caps_backlog,
        test_relay_measures_real_queue_depth_not_our_writes,
        test_relay_reports_unmeasurable_instead_of_guessing,
        test_relay_sink_missing_binary_says_how_to_build,
        test_relay_snapshot_reaches_the_panel,
        test_voiceprint_only_does_not_report_relay_as_live,
        test_main_starts_with_relay_attached,
        test_control_acknowledgement_is_tracked,
        test_serial_source_survives_transient_read_errors,
        test_sse_endpoint_streams,
        test_server_refuses_control_without_device,
        test_capture_event_seq_is_in_snapshot,
        test_control_ack_requires_a_new_capture_event,
        test_stop_cleanup_is_idempotent_and_runs_before_exit,
        test_voiceprint_decision_telemetry,
        test_assistant_snapshot_is_truthful_without_asr,
        test_assistant_rolls_continuous_speech_into_asr_segments,
        test_assistant_boosts_only_quiet_asr_copy,
        test_assistant_filters_asr_noise_hallucinations,
        test_assistant_interaction_features,
        test_assistant_gates_asr_on_speech_evidence,
        test_assistant_fast_path_authorization_and_late_veto,
        test_assistant_splits_unknown_speakers_into_pause_turns,
        test_assistant_aggregates_speaker_labels_per_turn,
        test_training_writer_records_segment_and_llm_context,
        test_assistant_uses_absolute_sample_offset_for_speakers,
        test_authorization_epoch_advances_after_rolling_cap_then_silence,
        test_assistant_asr_death_falls_back_to_fast_path,
        test_llm_ambient_authority_helpers,
        test_ambient_requires_fresh_other_voiceprint,
        test_continuous_audio_writes_one_contiguous_wav,
        test_continuous_audio_rotation_keeps_samples_contiguous,
        test_continuous_audio_gaps_and_rate_mismatch_are_explicit,
        test_continuous_audio_stops_at_disk_budget,
        test_continuous_audio_cli_defaults,
        test_main_wires_continuous_audio_from_replay,
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
