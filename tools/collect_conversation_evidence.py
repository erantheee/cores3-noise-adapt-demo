#!/usr/bin/env python3
"""采集 Apple 式对话感知的**触发标定素材**：用设备自己的麦克风，按"自己说话 / 别人说话"分家。

为什么单独一套素材
------------------
现有 `记录/声音类别/` 是给 24 段语音判据录的，而且用的是 **Mac 麦克风**（见状态总表 38h：
那些数字不能判定设备）。对话感知要回答的是另一个问题：

    这段声音是"**佩戴者自己**在说话"，还是"附近有人说话"？

所以类别必须按**谁在说**分家。判据本身（300–4000 Hz）在 16 kHz 素材上完全够用；
但注意**全 24 段（到 13.6 kHz）不能用这批素材判定** —— 串口 48 kHz 需要 128 kB/s，
可用吞吐只有 92 kB/s（README「端侧录音」一节），物理上就传不了。

用法
----
    python3 tools/collect_conversation_evidence.py --port /dev/cu.usbmodem1101 --speaker 甲
    # 只采两类，每类两遍
    python3 tools/collect_conversation_evidence.py --port ... --only self_speech_near,other_speech_1m --take 2
    # 采完立刻回放验证（会编译 tools/conversation_replay.cpp）
    python3 tools/collect_conversation_evidence.py --port ... --verify

提示用 `say` 念出来，对着设备时不用盯屏幕。每条录完立即报电平并与本次房间本底比。
**有丢块的录音直接作废**（时间断裂会污染电平分布），脚本会明确报出来并让你重录。
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from record_from_device import Recorder  # noqa: E402  复用分块解析与丢块统计

DEVICE_RATE = 16000
BAUD = 921600
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO_ROOT / "记录/对话感知标定"

# (目录, 念出来的提示, 录几秒, 建议条数, 这一条要放进 sidecar 的口径说明)
CLASSES: list[tuple[str, str, float, int, str]] = [
    ("silence", "保持安静，不要出声，也不要动设备。", 4.0, 3,
     "房间本底：所有电平比较的基线"),
    ("self_speech_near", "把设备拿到嘴边十到二十厘米，正常说一句话。", 5.0, 4,
     "自身语音正样本（近场）：对话感知**应该**触发"),
    ("self_speech_far", "把设备放回桌上，站在一米外正常说一句话。", 5.0, 3,
     "自身语音但远场：距离一变电平就变，近场代理在这里会失效"),
    ("other_speech_near", "请另一个人靠近设备十到二十厘米，说一句话。", 5.0, 4,
     "他人语音近场：**不应该**触发（这是与自身语音最难分的一类）"),
    ("other_speech_1m", "请另一个人在离设备一米处说一句话。", 5.0, 4,
     "他人语音远场：不应该触发"),
    ("podcast", "用手机或音箱在离设备一米处放一段播客或视频，放五秒。", 6.0, 3,
     "像对话但不是你：不应该触发（旧轮次启发式会在这里误触发）"),
    ("throat_clear", "清一下嗓子，用平常的力度。", 3.0, 3,
     "已知会误触发的一类（Apple 自己的用户抱怨里也有清嗓/咳嗽）"),
    ("hum_long", "闭着嘴持续哼，哼大概两秒。", 4.0, 3,
     "非语音的准周期声：不应该触发"),
    ("breath", "对着设备正常呼吸。", 5.0, 3,
     "气流声：不应该触发（历史上推翻过'呼吸安全'的合成结论）"),
]


def say(text: str) -> None:
    if shutil.which("say"):
        subprocess.run(["say", "-v", "Tingting", text], check=False)
    else:
        print(f"（无 say）{text}")


def dbfs(frames: bytes) -> float:
    if not frames:
        return -120.0
    total = 0.0
    count = len(frames) // 2
    for i in range(count):
        value = int.from_bytes(frames[2 * i:2 * i + 2], "little", signed=True)
        total += (value / 32768.0) ** 2
    rms = math.sqrt(total / max(count, 1))
    return 20.0 * math.log10(max(rms, 1e-7))


def record_from_device(port: str, seconds: float, tail: float = 1.5) -> Recorder:
    """向设备发 `rec start N`，在同一个串口上读回分块。

    必须同一个进程同一个端口：先发命令再读，两个进程抢同一个串口只会拿到空数据。
    DTR/RTS 必须拉高 —— ESP32-S3 的内置 USB Serial/JTAG 在 DTR 未置位时根本不发数据，
    这是 tools/record_from_device.py 里已经踩过一次的坑。
    """
    import serial  # noqa: PLC0415

    recorder = Recorder(DEVICE_RATE)
    connection = serial.Serial(port=port, baudrate=BAUD, timeout=0.2)
    connection.dtr = True
    connection.rts = True
    try:
        time.sleep(0.3)
        connection.reset_input_buffer()
        connection.write(f"rec start {seconds:g}\n".encode())
        connection.flush()
        deadline = time.monotonic() + seconds + tail
        while time.monotonic() < deadline:
            raw = connection.readline()
            if not raw:
                continue
            if not recorder.feed(raw.decode("utf-8", errors="replace")):
                recorder.bad_lines += 1
        connection.write(b"rec stop\n")
        connection.flush()
    finally:
        connection.close()
    return recorder


def collect(args) -> list[dict]:
    wanted = [c for c in CLASSES if args.only is None or c[0] in args.only]
    if not wanted:
        print("!! --only 没有匹配到任何类别")
        return []

    date = datetime.date.today().strftime("%Y%m%d")
    takes: list[dict] = []
    floor: float | None = None

    for name, prompt, seconds, suggested, note in wanted:
        count = args.take or suggested
        for index in range(1, count + 1):
            print(f"\n=== {name} {index}/{count} ===")
            print(f"提示：{prompt}")
            say(prompt)
            time.sleep(min(seconds * 0.6, 3.0))
            recorder = record_from_device(args.port, seconds)
            level = dbfs(bytes(recorder.frames))
            ok = not recorder.gaps and recorder.seconds >= seconds * 0.8
            status = "✅" if ok else ("⚠️ 丢块" if recorder.gaps else "⚠️ 太短")
            print(f"  {status} {recorder.seconds:.2f}s / {recorder.chunks} 块 / 电平 {level:.1f} dBFS")
            if recorder.gaps:
                print("  丢块 → 这一段作废，请重录（时间断裂会让电平分布失真）")
            if name == "silence" and ok and floor is None:
                floor = level
            if floor is not None and name != "silence":
                print(f"  相对本底 {level - floor:+.1f} dB（本底 {floor:.1f}）")

            path = OUT_ROOT / name / f"{date}-{name}-{index:02d}.wav"
            recorder.write_wav(path)
            sidecar = {
                "category": name,
                "index": index,
                "speaker": args.speaker,
                "seconds": round(recorder.seconds, 3),
                "chunks": recorder.chunks,
                "gaps": len(recorder.gaps),
                "level_dbfs": round(level, 2),
                "room_floor_dbfs": None if floor is None else round(floor, 2),
                "usable": bool(ok),
                "note": note,
                "prompt": prompt,
                "device_rate_hz": DEVICE_RATE,
                "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            path.with_suffix(".json").write_text(json.dumps(sidecar, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
            takes.append(sidecar)
    return takes


def verify(takes: list[dict]) -> None:
    """采完立刻回放：这是判断"近场代理能不能分开自己/别人"的唯一诚实方式。"""
    root = REPO_ROOT
    binary = Path("/tmp/convreplay")
    source = root / "tools/conversation_replay.cpp"
    build = subprocess.run(
        ["g++", "-std=c++17", "-O2", "-I", "main", "-o", str(binary), str(source),
         "main/controller.cpp", "main/conversation_awareness.cpp"],
        cwd=root, capture_output=True, text=True)
    if build.returncode != 0:
        print("!! 回放工具编译失败：\n" + build.stderr[-800:])
        return
    print(f"\n{'类别':<20}{'拍数':>6}{'语音拍':>8}{'下压拍':>8}{'最低%':>7}{'own峰值':>9}  判定")
    for path in sorted(OUT_ROOT.rglob("*.wav")):
        run = subprocess.run([str(binary), str(path.resolve().relative_to(root)), "--row"],
                             cwd=root, capture_output=True, text=True)
        row = run.stdout.strip().splitlines()[-1].split("|") if run.stdout.strip() else []
        if len(row) < 11:
            continue
        _, _, ticks, _, _, _, dmin, opeak, sticks, eticks, verdict = row[:11]
        print(f"{path.parent.name:<20}{ticks:>6}{sticks:>8}{eticks:>8}{dmin:>7}{opeak:>9}  {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="设备端对话感知触发标定素材采集")
    parser.add_argument("--port", required=True, help="串口，例如 /dev/cu.usbmodem1101")
    parser.add_argument("--speaker", default="甲", help="说话人标签，写进 sidecar")
    parser.add_argument("--only", default=None, help="只采这些类别，逗号分隔")
    parser.add_argument("--take", type=int, default=0, help="每类录几条（默认按类别建议值）")
    parser.add_argument("--verify", action="store_true", help="采完立刻回放验证")
    parser.add_argument("--verify-only", action="store_true",
                        help="不采集，只对 记录/对话感知标定/ 下已有素材跑回放验证")
    args = parser.parse_args()
    args.only = None if args.only is None else {c.strip() for c in args.only.split(",")}

    if args.verify_only:
        verify([])
        return 0

    print(__doc__.split("用法")[0].strip())
    takes = collect(args)
    if not takes:
        return 1

    usable = [t for t in takes if t["usable"]]
    print(f"\n本会话：{len(takes)} 条，可用 {len(usable)} 条 → {OUT_ROOT}")
    if len(usable) != len(takes):
        print("⚠️ 有作废条目，标定前请重录（作废的 JSON 里 usable=false）")
    if args.verify:
        verify(takes)
    else:
        print("\n回放验证：给本脚本加 --verify，或手动跑")
        print("  g++ -std=c++17 -O2 -I main -o /tmp/convreplay tools/conversation_replay.cpp \\")
        print("      main/controller.cpp main/conversation_awareness.cpp")
        print("  /tmp/convreplay 记录/对话感知标定/self_speech_near/<某个.wav>")
    return 0 if len(usable) == len(takes) else 2


if __name__ == "__main__":
    raise SystemExit(main())
