#!/usr/bin/env python3
"""测一个播放器会不会把音频吞进自己的内部队列。

为什么需要这个判据：实时中继的延迟由**最慢的那个缓冲区**决定，而不是由我们自己那个队列
决定。如果一个播放器在内部一次囤下好几秒音频，我们这边把队列从 6 块砍到 2 块也不会
让声音早出来——症状就是「改了参数，延迟只动一点点」，而我们就一直在错误的地方调参。

判据是反压：按实时速率写一段音频，如果播放器按时长消耗，写入耗时应该约等于音频时长；
如果它一次吞下去，写入会在极短时间里全部完成。吸收比 = 写进音频时长 ÷ 实际写入耗时。

    吸收比 ≈ 1     播放器按实时速率反压，我们的队列就是真正的延迟
    吸收比 ≫ 1     播放器在内部囤数据，延迟由它的缓冲深度决定

用法：
    python3 tools/probe_player_buffer.py                    # 测当前中继用的 ffplay 参数
    python3 tools/probe_player_buffer.py --seconds 5
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time

RATE = 16000
CHUNK_SECONDS = 0.02
CHUNK_BYTES = int(RATE * CHUNK_SECONDS) * 2


def relay_ffplay_command() -> list[str]:
    """与 tools/host/audio_relay.py 里实际使用的那条命令保持一致。

    这里刻意复制而不是 import：如果以后有人改了中继的参数却忘了这里，探针会测到一个
    已经不存在的播放器，结论就失效了。两处不一致时以 audio_relay.py 为准。
    """
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        raise SystemExit("找不到 ffplay")
    return [
        ffplay, "-nodisp", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay", "-avioflags", "direct",
        "-probesize", "32", "-analyzeduration", "0",
        "-f", "s16le", "-ar", str(RATE), "-ch_layout", "mono",
        "-af", "volume=18dB,alimiter=limit=0.95",
        "-i", "pipe:0",
    ]


def probe(command: list[str], seconds: float) -> int:
    total_chunks = int(seconds / CHUNK_SECONDS)
    silence = bytes(CHUNK_BYTES)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    started = time.monotonic()
    written = 0
    try:
        for _ in range(total_chunks):
            process.stdin.write(silence)
            process.stdin.flush()
            written += 1
    except (BrokenPipeError, OSError) as exc:
        print(f"写入中断：{exc}", file=sys.stderr)
    elapsed = time.monotonic() - started
    audio_seconds = written * CHUNK_SECONDS
    absorbed = audio_seconds / elapsed if elapsed > 0 else float("inf")

    print(f"写入音频      {audio_seconds:.2f} s（{written} 块 × {CHUNK_SECONDS * 1000:.0f} ms）")
    print(f"实际耗时      {elapsed:.2f} s")
    print(f"吸收比        {absorbed:.1f}×")
    if absorbed >= 3.0:
        print("结论：播放器在内部囤数据，实时中继的延迟由它的缓冲决定。")
    elif absorbed >= 1.3:
        print("结论：写入比实时快一些，播放器有一定预读，但不是主要延迟来源。")
    else:
        print("结论：播放器按实时速率反压，我们自己的队列就是真正的延迟。")

    process.stdin.close()
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="测播放器内部缓冲（吸收比判据）")
    parser.add_argument("--seconds", type=float, default=5.0, help="写入多少秒音频")
    parser.add_argument("--player", default=None,
                        help="自定义播放器命令（用空格分隔）；默认用中继的 ffplay 参数")
    args = parser.parse_args(argv)
    command = args.player.split() if args.player else relay_ffplay_command()
    print(f"播放器：{' '.join(command)}")
    return probe(command, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
