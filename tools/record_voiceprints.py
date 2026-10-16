#!/usr/bin/env python3
"""给声纹止损门（tools/voice_separability.py）录素材。

止损门要的不是"好听的一段话"，是**条件故意变化**的几条：
只在一个条件下录出来的"稳定"声纹，在真实使用中不成立——姊妹项目就是栽在这里。

所以这个脚本按固定协议走三档条件，每条 10 秒：

    1. 正常距离（约 1 米）、正常音量
    2. 远一点（2–3 米）、小声
    3. 近一点（20–30 厘米）、大声

**同一个人要在另一天再跑一次**，两条日期都要进 --enroll。分两天是判据的一部分：
类内抖动算的就是"同一个人换条件、换时间之后还像不像自己"。

另一人（impostor）同样录 3 条，一天也行。

用法
----
    # 甲，第一天
    python3 tools/record_voiceprints.py --speaker 甲 --out 记录/声纹/甲
    # 甲的第二天（同样的命令，再跑一次）
    python3 tools/record_voiceprints.py --speaker 甲 --out 记录/声纹/甲
    # 乙
    python3 tools/record_voiceprints.py --speaker 乙 --out 记录/声纹/乙

    # 跑判据
    python3 tools/voice_separability.py \\
        --enroll-dir 记录/声纹/甲 --impostor-dir 记录/声纹/乙 --json 记录/声纹/result.json

文件名自带日期和条件，所以一个目录里可以叠很多天的录音，工具会全收。
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
BYTES_PER_SAMPLE = 2

#: 本底多高算"这个房间不适合录"。超过就该换个地方，而不是把门槛抬高硬录。
#: 依据：正常说话的 RMS 大致在 0.01–0.03，本底到 0.006 就已经只差一档了。
AMBIENT_WARN = 0.006

# (标签, 提示, 合格线系数)
#
# 合格线 = 本次量到的房间本底 × 系数。**必须每次现量，不能硬编码**：
# 同一台 Mac 的内置麦，实测本底在 0.003 和 0.012 之间跳过（差 4 倍，随系统输入增益和环境变），
# 固定门槛要么收下静音、要么误杀小声说话——这两种错在开发中各发生过一次。
#
# 系数按条件分档，因为三档本来就是刻意制造的音量差：用一个统一倍数，"远小声"必然被误杀。
CONDITIONS: list[tuple[str, str, float]] = [
    ("正常", "正常距离（约 1 米），正常音量说话", 2.5),
    ("远小声", "退到 2–3 米外，故意小声说", 1.6),
    ("近大声", "凑到 20–30 厘米，正常偏大地声说", 2.5),
]

READ_HINT = "随便念点什么：一段新闻、一段书、报数字都行，别只哼一声。"


def pcm_rms(path: Path, window_s: float = 0.25) -> float:
    """读裸 PCM 的尾部算 RMS。录制过程中也能读——wav 的头要等 ffmpeg 退出才写。"""
    nbytes = int(window_s * RATE) * BYTES_PER_SAMPLE
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


def measure_ambient(device: str, seconds: float = 3.0) -> float:
    """先量一遍房间本底。

    **不能用固定阈值判断"有没有说话"**：同一台 Mac 的内置麦，实测本底在 0.003 和 0.012
    之间跳过（差 4 倍，随系统输入增益和环境变），任何硬编码的门槛不是收下静音就是误杀
    小声说话。所以每条录音的合格线是"这次量到的本底 × 该条件的系数"（见 CONDITIONS）。
    """
    pcm = Path("/tmp/_voiceprint_ambient.pcm")
    pcm.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
         "-f", "avfoundation", "-i", device,
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-y", str(pcm)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print("\n先量 3 秒房间本底，请保持安静…")
    time.sleep(seconds + 1.0)
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    # 整段算，不用尾部窗口：ffmpeg 的启动开销会让实际落盘长度短于 seconds，
    # 尾部窗口的守卫会直接返回 0（第一版就是这么把本底读成 0.00000 的）。
    try:
        raw = pcm.read_bytes()
        s = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        value = float(np.sqrt((s ** 2).mean())) if s.size else 0.0
    except OSError:
        value = 0.0
    pcm.unlink(missing_ok=True)
    return value


def record_one(path: Path, seconds: float, device: str, instruction: str,
               threshold: float) -> bool:
    """录一条，带实时电平。返回是否成功。

    倒计时**在开录之前**：早先的版本先起 ffmpeg 再倒计时，结果每条前面都多录了 3 秒
    房间本底，文件长度也不是 --seconds。
    """
    pcm = path.with_suffix(".pcm")
    print(f"\n  ▶ {instruction}")
    print(f"    {READ_HINT}")
    print("    3 秒后开始…")
    for i in (3, 2, 1):
        print(f"\r    {i}", end="", flush=True)
        time.sleep(1.0)
    print("\r    开始！      ", flush=True)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
        "-f", "avfoundation", "-i", device,
        "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-y", str(pcm),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # 等 ffmpeg 真的往文件里写出第一段数据再开始计时。
    # avfoundation 打开麦克风要 1 秒以上，固定 sleep 要么太短（整段读到 0）、要么白录一段。
    ready_deadline = time.monotonic() + 5.0
    while time.monotonic() < ready_deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            print(f"⚠️  录音没起来：{err.splitlines()[-1] if err else 'ffmpeg 立刻退出'}",
                  file=sys.stderr)
            print("    多数是终端没有麦克风权限：系统设置 → 隐私与安全性 → 麦克风",
                  file=sys.stderr)
            return False
        try:
            if pcm.exists() and pcm.stat().st_size > RATE * BYTES_PER_SAMPLE // 4:
                break
        except OSError:
            pass
        time.sleep(0.05)
    else:
        print("⚠️  等了 5 秒 ffmpeg 还没写出音频，放弃这一条", file=sys.stderr)
        proc.kill()
        return False

    end = time.monotonic() + seconds
    levels: list[float] = []
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        lv = pcm_rms(pcm)
        levels.append(lv)
        bar = "█" * int(min(lv * 500, 40))
        print(f"\r    {left:4.1f}s  {bar:<40}", end="", flush=True)
        time.sleep(0.1)

    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()

    mean = float(np.mean(levels)) if levels else 0.0
    if mean < threshold:
        print(f"\r    完成 —— ⚠️ 电平 {mean:.5f} 没过线（门槛 {threshold:.5f}），"
              f"基本没有语音，这一条作废重录")
        pcm.unlink(missing_ok=True)
        return False

    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(RATE)
        w.writeframes(pcm.read_bytes())
    pcm.unlink(missing_ok=True)
    dur = path.stat().st_size / (RATE * BYTES_PER_SAMPLE)
    print(f"\r    完成 —— {dur:.1f} 秒，电平 {mean:.5f} → {path.name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="给声纹止损门录音")
    ap.add_argument("--speaker", required=True, help="说话人代号，例如 甲 / 乙")
    ap.add_argument("--out", type=Path, required=True, help="输出目录")
    ap.add_argument("--seconds", type=float, default=10.0, help="每条录几秒")
    ap.add_argument("--audio-device", default=":0", help="ffmpeg avfoundation 输入")
    ap.add_argument("--rounds", type=int, default=1,
                    help="一次跑几轮（每轮给文件名加后缀，方便同一天多录几组）")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        print("找不到 ffmpeg。", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d")
    print(f"说话人 {args.speaker} → {args.out}")
    print(f"今天 {today}。协议：{len(CONDITIONS)} 条 × {args.seconds:.0f} 秒，"
          f"每条条件不同。")
    print("注意：止损门要求**同一个人在不同日期**各录一轮，两轮的录音都要留在同一个目录里。")

    ambient = measure_ambient(args.audio_device)
    if ambient <= 0:
        print("⚠️  本底读数为 0：麦克风多半没起来，先解决权限再继续。", file=sys.stderr)
        return 1
    print(f"房间本底 RMS = {ambient:.5f}")
    if ambient > AMBIENT_WARN:
        print(f"⚠️  本底偏高（>{AMBIENT_WARN}）。正常说话大约在 0.01–0.03，"
              f"本底到这个量级会明显拉低声纹质量。\n"
              f"    建议换个安静点的房间或关掉风机再录 —— 不是把门槛抬高硬录。",
              file=sys.stderr)

    written: list[Path] = []
    for rnd in range(1, args.rounds + 1):
        if args.rounds > 1:
            print(f"\n──── 第 {rnd}/{args.rounds} 轮 ────")
        for idx, (tag, instruction, factor) in enumerate(CONDITIONS, start=1):
            suffix = "" if args.rounds == 1 else f"-r{rnd}"
            path = args.out / f"{today}-{args.speaker}-{tag}{suffix}.wav"
            if path.exists():
                print(f"\n  跳过已存在：{path.name}")
                continue
            threshold = max(ambient * factor, 1e-4)
            print(f"\n[{idx}/{len(CONDITIONS)}] {path.name}  "
                  f"（合格线 {threshold:.5f} = 本底 ×{factor}）")
            if not record_one(path, args.seconds, args.audio_device, instruction, threshold):
                print("  这一条没录成，中断。修好后再跑一次即可（已录的会跳过）。")
                return 1
            written.append(path)

    print(f"\n本次写入 {len(written)} 条：")
    for p in written:
        print(f"  {p}")
    have = sorted(args.out.glob("*.wav"))
    days = sorted({p.name.split("-")[0] for p in have})
    print(f"\n目录里现在共 {len(have)} 条，覆盖 {len(days)} 个日期：{', '.join(days)}")
    if len(days) < 2:
        print("⚠️  只有一天。止损门要的是**跨天**的类内抖动 —— 换一天再跑一次同样的命令。")
    print("\n所有人录完后跑判据：")
    print("  python3 tools/voice_separability.py \\")
    print("      --enroll-dir 记录/声纹/甲 --impostor-dir 记录/声纹/乙 \\")
    print("      --json 记录/声纹/result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
