#!/usr/bin/env python3
"""给语音判据录真实素材：清嗓子 / 短哼 / 持续哼 / 呼吸 / 说话 / 静音。

为什么要录：合成信号只能证明机制的**形状**。真实素材已经推翻过一个合成结论
（"呼吸声安全"——那是只取一帧、正好采在气流最弱处的假象），所以每一条阈值主张
最终都得落到真人录音上。

**提示是用 `say` 念出来的**，不是打在屏幕上 —— 你对着麦克风的时候不用盯着终端。

每条录完会立即报电平，并和本次量到的房间本底比。太轻的当场作废重录，不让你白录一轮。

用法
----
    python3 tools/record_sound_classes.py --speaker 甲 --out 记录/声音类别
    # 只做其中几类
    python3 tools/record_sound_classes.py --speaker 甲 --out 记录/声音类别 --only throat_clear,hum_short

录完：
    g++ -std=c++17 -O2 -I main -o /tmp/vdoffline tools/voice_decision_offline.cpp main/controller.cpp
    /tmp/vdoffline 记录/声音类别/throat_clear/xxx.wav

⚠️ 麦克风口径：这用的是 **Mac 麦克风**，不是设备的 ES7210。判据的阈值是在设备上标定的，
   所以这批素材能判断**机制对不对**，不能直接搬阈值。要设备口径得让 ear-context 固件把
   48 kHz 原始音频吐出来（现在的设备端录音通路只给 16 kHz 的语音中继速率，24 个频段
   到 13.6 kHz，不够用）。
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

RATE = 48000
BYTES = 2

# (目录名, 念出来的提示, 录几秒, 建议条数)
CLASSES: list[tuple[str, str, float, int]] = [
    ("silence",     "保持安静，不要出声。", 4.0, 3),
    ("throat_clear", "清一下嗓子，用平常的力度。可以连着清两下。", 3.0, 4),
    ("hum_short",   "短促地嗯一声，像答应别人那样。", 3.0, 4),
    ("hum_long",    "闭着嘴持续哼，哼大概两秒。", 4.0, 3),
    ("breath",      "对着麦克风正常呼吸，鼻子吸气嘴巴呼气。", 5.0, 3),
    ("speech",      "正常说一句话，随便说什么都行。", 4.0, 4),
]


def say(text: str) -> None:
    subprocess.run(["say", "-v", "Tingting", text], check=False)


def pcm_rms(path: Path, window_s: float = 0.25) -> float:
    nbytes = int(window_s * RATE) * BYTES
    try:
        size = path.stat().st_size
        if size < nbytes * 2:
            return 0.0
        with path.open("rb") as fh:
            fh.seek(size - nbytes)
            s = np.frombuffer(fh.read(nbytes), dtype="<i2").astype(np.float64) / 32768.0
        return float(np.sqrt((s ** 2).mean())) if s.size else 0.0
    except OSError:
        return 0.0


def measure_ambient(seconds: float = 3.0) -> float:
    pcm = Path("/tmp/_soundclass_ambient.pcm")
    pcm.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-f", "avfoundation",
         "-i", ":0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-y", str(pcm)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print("先量 3 秒房间本底，保持安静…")
    time.sleep(seconds + 1.2)
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    try:
        s = np.frombuffer(pcm.read_bytes(), dtype="<i2").astype(np.float64) / 32768.0
        value = float(np.sqrt((s ** 2).mean())) if s.size else 0.0
    except OSError:
        value = 0.0
    pcm.unlink(missing_ok=True)
    return value


def record_one(path: Path, seconds: float, spoken: str, ambient: float,
               silent_class: bool) -> bool:
    say(spoken)
    print(f"    ▶ {spoken}")
    pcm = path.with_suffix(".pcm")
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-f", "avfoundation",
         "-i", ":0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-y", str(pcm)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # 等 ffmpeg 真写出数据再计时：avfoundation 打开麦克风要 1 秒以上，
    # 固定 sleep 要么整段读到 0、要么白录一段（两个都踩过）。
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            print(f"    ⚠️ 录音没起来：{err.splitlines()[-1] if err else 'ffmpeg 退出'}",
                  file=sys.stderr)
            return False
        if pcm.exists() and pcm.stat().st_size > RATE * BYTES // 4:
            break
        time.sleep(0.05)
    else:
        print("    ⚠️ 等了 5 秒还没音频，跳过", file=sys.stderr)
        proc.kill()
        return False

    end = time.monotonic() + seconds
    levels: list[float] = []
    while time.monotonic() < end:
        levels.append(pcm_rms(pcm))
        print(f"\r      {max(0.0, end - time.monotonic()):4.1f}s", end="", flush=True)
        time.sleep(0.1)
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()

    mean = float(np.mean(levels)) if levels else 0.0
    ratio = mean / ambient if ambient > 0 else 0.0
    # 静音类是**要求**接近本底的；其余类必须明显高于本底。
    # 门槛是相对的：本底实测在 0.003 和 0.012 之间跳过，固定阈值两次都错过。
    if silent_class:
        ok = ratio < 2.0
        expect = "应接近本底"
    else:
        ok = ratio >= 1.6
        expect = "应高于本底"
    if not ok:
        print(f"\r      ⚠️ 电平 {mean:.5f} = 本底 {ratio:.2f}×（{expect}），作废重录")
        pcm.unlink(missing_ok=True)
        return False

    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(BYTES)
        w.setframerate(RATE)
        w.writeframes(pcm.read_bytes())
    pcm.unlink(missing_ok=True)
    print(f"\r      完成 {mean:.5f} = 本底 {ratio:.2f}× → {path.name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="录语音判据的真实素材")
    ap.add_argument("--speaker", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", default=None, help="只录这几类，逗号分隔")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        print("找不到 ffmpeg。", file=sys.stderr)
        return 2

    wanted = set(args.only.split(",")) if args.only else None
    classes = [c for c in CLASSES if wanted is None or c[0] in wanted]
    if not classes:
        print(f"--only 里没有匹配的类别。可选：{', '.join(c[0] for c in CLASSES)}", file=sys.stderr)
        return 2

    ambient = measure_ambient()
    if ambient <= 0:
        print("⚠️ 本底读数为 0：麦克风没起来，先解决权限。", file=sys.stderr)
        return 1
    print(f"房间本底 RMS = {ambient:.5f}\n")

    today = datetime.datetime.now().strftime("%Y%m%d")
    say("开始录音。每一步我会念出来，你照着做就行。")
    written = 0
    for name, spoken, seconds, takes in classes:
        d = args.out / name
        d.mkdir(parents=True, exist_ok=True)
        have = len(list(d.glob("*.wav")))
        need = max(0, takes - have)
        print(f"[{name}] 已有 {have} 条，本次录 {need} 条")
        say(f"接下来录{name}，一共{need}条。")
        for i in range(need):
            path = d / f"{today}-{args.speaker}-{have + i + 1:02d}.wav"
            print(f"  第 {i + 1}/{need} 条")
            if not record_one(path, seconds, spoken, ambient, name == "silence"):
                print("  这一条没录成，跳过。")
        written += need

    say("录音结束。")
    total = sum(len(list(p.glob('*.wav'))) for p in args.out.iterdir() if p.is_dir())
    print(f"\n→ {args.out}，共 {total} 条")
    print("下一步（逐条过判据）：")
    print("  g++ -std=c++17 -O2 -I main -o /tmp/vdoffline "
          "tools/voice_decision_offline.cpp main/controller.cpp")
    print("  /tmp/vdoffline 记录/声音类别/throat_clear/<某个.wav>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
