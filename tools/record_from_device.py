#!/usr/bin/env python3
"""从 CoreS3 串口录音，落成 WAV。

设备以 16 kHz 单声道输出 base64 分块，每块一行 JSON。本脚本负责解码、落盘，并**报告丢块**——
丢块会让录音出现时间断裂，声纹的类内抖动和信噪比都会被污染，所以丢块必须显式报出来，
不能静默补零。

用法
----
    # 从串口录，按秒数自动停止
    python3 tools/record_from_device.py --port /dev/cu.usbmodem1101 --seconds 10 \\
        --out 记录/甲-安静.wav

    # 已有日志文件（离线重放）
    python3 tools/record_from_device.py --file capture.log --out 记录/甲-安静.wav

    # 只看设备是否在发录音块，不落盘
    python3 tools/record_from_device.py --port /dev/cu.usbmodem1101 --dry-run

录之前需要先让设备开始录（见 README 的「端侧录音」一节）。

依赖：pyserial（仅串口模式需要）。缺失时明确报缺，不给假结果。
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time
import wave
from pathlib import Path

# 仅用于 dry-run 的提示性检查；真正的判定在 parse_chunk 里
CHUNK_MARKER = "mic_chunk"


class MissingDependency(Exception):
    pass


def parse_chunk(line: str):
    """解析一行录音块。返回 (序号, 采样字节) 或 None。

    先做一次便宜的字符检查再解析：设备每 100 ms 也会输出 earbud_command 行，而录音时
    日志量很大，不值得为每一行做完整 JSON 解析。但检查只用来快速排除，判定仍以解析出的
    类型字段为准——真实数据不该因为冒号后面多个空格就被静默丢掉。
    """
    line = line.strip()
    if not line or "mic_chunk" not in line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "mic_chunk":
        return None
    encoded = payload.get("pcm")
    if not isinstance(encoded, str):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if len(raw) % 2 != 0:
        return None
    return payload.get("sequence"), raw


class Recorder:
    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.frames = bytearray()
        self.chunks = 0
        self.expected_sequence = None
        self.gaps: list[tuple[int, int]] = []
        self.bad_lines = 0

    def feed(self, line: str) -> bool:
        parsed = parse_chunk(line)
        if parsed is None:
            return False
        sequence, raw = parsed
        if isinstance(sequence, int):
            if self.expected_sequence is not None and sequence != self.expected_sequence:
                self.gaps.append((self.expected_sequence, sequence))
            self.expected_sequence = sequence + 1
        self.frames.extend(raw)
        self.chunks += 1
        return True

    @property
    def seconds(self) -> float:
        return len(self.frames) / 2 / self.sample_rate

    def write_wav(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(bytes(self.frames))

    def report(self, path: Path | None) -> None:
        print(f"  时长      {self.seconds:.2f} s")
        print(f"  采样      {len(self.frames) // 2} 点 @ {self.sample_rate} Hz")
        print(f"  块数      {self.chunks}")
        if self.gaps:
            lost = sum(b - a for a, b in self.gaps)
            print(f"  丢块      {len(self.gaps)} 处，共 {lost} 块  <-- 录音有断裂")
            print("            丢块会让声纹抖动与信噪比失真，这一段录音不可用于结论")
        else:
            print("  丢块      无")
        if self.bad_lines:
            print(f"  无法解析  {self.bad_lines} 行（非录音行或已损坏）")
        if path:
            print(f"  已写入    {path}")


def read_serial(port: str, baud: int, seconds: float, recorder: Recorder, dry_run: bool):
    try:
        import serial  # noqa: PLC0415
    except ImportError as exc:
        raise MissingDependency(
            "缺少 pyserial。安装：pip install pyserial\n"
            "或复用仓库已有的工具环境（见 README 的本地连接一节）。"
        ) from exc

    connection = serial.Serial(port=port, baudrate=baud, timeout=0.2)
    # Match tools/monitor.sh: assert DTR/RTS. The ESP32-S3's built-in USB Serial/JTAG
    # does not transmit while DTR is deasserted -- this used to be `dtr = False`, which
    # made the port open cleanly and then deliver nothing at all, for any duration.
    connection.dtr = True
    connection.rts = True
    print(f"监听 {port} @ {baud}，最多 {seconds:g} 秒（Ctrl+C 提前结束）")
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            raw = connection.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            if dry_run:
                if CHUNK_MARKER in line:
                    print("  收到录音块", flush=True)
                continue
            if not recorder.feed(line):
                recorder.bad_lines += 1
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        connection.close()


def read_file(path: Path, recorder: Recorder):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not recorder.feed(line):
                recorder.bad_lines += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="从 CoreS3 串口录音，落成 WAV")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", help="串口设备，例如 /dev/cu.usbmodem1101")
    source.add_argument("--file", help="已有的串口日志，离线重放")
    parser.add_argument("--baud", type=int, default=921600, help="波特率，默认 921600")
    parser.add_argument("--seconds", type=float, default=30.0, help="最长录制秒数")
    parser.add_argument("--out", default=None, help="输出 WAV 路径")
    parser.add_argument("--rate", type=int, default=16000, help="采样率，需与设备一致")
    parser.add_argument("--dry-run", action="store_true", help="只确认设备在发录音块，不落盘")
    args = parser.parse_args()

    if args.dry_run and not args.port:
        print("--dry-run 需要 --port", file=sys.stderr)
        return 2

    recorder = Recorder(args.rate)
    try:
        if args.port:
            read_serial(args.port, args.baud, args.seconds, recorder, args.dry_run)
        else:
            path = Path(args.file)
            if not path.exists():
                print(f"找不到 {path}", file=sys.stderr)
                return 2
            read_file(path, recorder)
    except MissingDependency as exc:
        print(f"依赖未就位：\n\n{exc}", file=sys.stderr)
        return 3

    if args.dry_run:
        print("dry-run 结束。" + ("设备在发录音块。" if recorder.chunks else "没看到录音块。"))
        return 0 if recorder.chunks else 1

    if recorder.chunks == 0:
        print(
            "没有收到任何录音块。先确认：\n"
            "  1. 设备已经进入录音状态（README 的「端侧录音」一节）\n"
            "  2. 串口速率是 921600（16 kHz 录音在 115200 下会丢块）\n"
            "  3. 端口没被 monitor.sh 之类的进程占用",
            file=sys.stderr,
        )
        return 1

    out = Path(args.out) if args.out else Path("recording.wav")
    recorder.write_wav(out)
    print("=== 录音结果 ===")
    recorder.report(out)
    if recorder.gaps:
        print("\n这一段录音有丢块，不要用于声纹或信噪比结论。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
