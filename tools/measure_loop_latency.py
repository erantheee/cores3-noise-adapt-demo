#!/usr/bin/env python3
"""测「Mac 放声 → 空气 → S3 麦克风 → USB → 主机」的往返延迟。

它回答的是一个具体问题：用户说「差到 1 秒」，这 1 秒里有多少是 AirPods 蓝牙与设备采集
的物理开销，有多少是我们自己在主机的队列与播放器缓冲里堆出来的。

方法：在主机侧对一个短促脉冲打时间戳 t0，让 Mac 从当前默认输出（AirPods）放出来；S3 的
麦克风在空气里听到它，并作为 mic_chunk 送回主机。第一个明显超过本底的音频块到达主机的
时刻减去 t0，就是这一整段的延迟。

为什么这个量能代表用户听到的延迟。用户感知的链是

    S3 采集 → USB → 主机队列 → 播放器缓冲 → AirPods 输出

本脚本测的是

    播放器(afplay)缓冲 → AirPods 输出 → 空气 → S3 采集 → USB → 主机读取

两条链共享 AirPods 输出、S3 采集与 USB，差别只在两个播放器各自的缓冲大小。因此这个数字
是这条物理路径的下限：如果它自己就已经接近 1 秒，那么换掉主机侧播放器也救不回来。

已知不确定度：afplay 拉起进程本身约几十毫秒，量在 t0 之后。分辨 1 秒与 0.5 秒够用，
报数时按「约」来读。

依赖：pyserial（与 tools/record_from_device.py 相同）。

用法：
    python3 tools/measure_loop_latency.py --port /dev/cu.usbmodem101
"""

from __future__ import annotations

import argparse
import array
import math
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host.protocol import parse_line  # noqa: E402

#: 脉冲用 44.1 kHz，避免播放端为它做重采样而引入额外的不确定度。
CLICK_RATE = 44100
CLICK_MS = 40
CLICK_HZ = 1200
CLICK_AMPLITUDE = 0.7
#: 中继模式用的音频参数，必须与固件的 kRelayRate / kCaptureChunkSamples 一致。
RELAY_RATE = 16000
RELAY_CHUNK_SAMPLES = 320


def build_click_pcm(rate: int) -> bytes:
    """和 make_click 同一个脉冲，但直接产出 s16le 字节，供中继播放器使用。"""
    count = int(rate * CLICK_MS / 1000)
    edge = max(1, int(rate * 0.004))
    samples = array.array("h")
    for i in range(count):
        env = min(1.0, i / edge, (count - i) / edge)
        samples.append(int(CLICK_AMPLITUDE * env * 32767 * math.sin(2 * math.pi * CLICK_HZ * i / rate)))
    return samples.tobytes()


class RelaySource:
    """用**真正的中继播放器**发声，测用户实际听到的那条路径。

    为什么不用 afplay 打脉冲：afplay 每响一次都是从冷启动拉起一条 A2DP 流，实测同一路径
    三次是 1035/1225/1095 ms、一次是 205 ms——差别全在蓝牙链路醒没醒，而不是在音频数据上。
    中继里音频是连续的（每 20 ms 一块），链路一直是热的，所以只有用它才能测到用户真正听到
    的延迟。
    """

    def __init__(self, rate: int, player: str = "sink") -> None:
        from host.audio_relay import AudioRelay  # noqa: PLC0415

        self.rate = rate
        # 播放端可换：sink（新，报真实排队深度）与 ffplay（旧，延迟不可观测）。
        # 换它而不是换别的，是因为"延迟到底降了多少"必须能归因到具体那一处改动。
        self.relay = AudioRelay(rate=rate, player=player)
        #: 脉冲真正进入中继队列的时刻，由 stream() 填写。
        self.click_at: float | None = None

    def start(self) -> None:
        self.relay.start()

    def stop(self) -> None:
        self.relay.stop()

    def stream(self, click_pcm: bytes, lead_in: float, total: float) -> None:
        """后台按实时速率喂静音，到点插入一次脉冲。"""
        period = RELAY_CHUNK_SAMPLES / self.rate
        silence = bytes(RELAY_CHUNK_SAMPLES * 2)
        start = time.monotonic()
        deadline = start + total
        target = start + lead_in
        sent = False
        next_tick = start
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(next_tick - now, 0.004))
                continue
            if not sent and now >= target:
                self.click_at = time.monotonic()
                self.relay.feed(click_pcm, self.rate)
                sent = True
            else:
                self.relay.feed(silence, self.rate)
            next_tick += period


def make_click(path: Path) -> int:
    """写一个带 5 ms 淡入淡出的短音，返回样本数。"""
    count = CLICK_RATE * CLICK_MS // 1000
    edge = CLICK_RATE * 0.005
    frames = bytearray()
    for i in range(count):
        env = min(1.0, i / edge, (count - i) / edge)
        value = int(
            CLICK_AMPLITUDE * env * 32767 * math.sin(2 * math.pi * CLICK_HZ * i / CLICK_RATE)
        )
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CLICK_RATE)
        handle.writeframes(bytes(frames))
    return count


def chunk_rms(pcm: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples))


class LineReader:
    """按行切分串口字节流。

    与 tools/host/sources.py 同一个理由：pyserial 的 read(size) 会等满 size，直接把
    读取粒度变成延迟。这里 in_waiting 为 0 时只读 1 字节，有数据就立刻返回。
    """

    def __init__(self, connection) -> None:  # noqa: ANN001
        self.connection = connection
        self.buffer = bytearray()

    def lines(self) -> list[str]:
        pending = self.connection.in_waiting
        raw = self.connection.read(pending if pending > 0 else 1)
        if raw:
            self.buffer += raw
        out: list[str] = []
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            out.append(line.decode("utf-8", "replace"))
        return out


def wait_for_capture(reader: LineReader, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in reader.lines():
            parsed = parse_line(line)
            if parsed.value is not None and getattr(parsed.value, "state", None) == "started":
                return True
    return False


def collect_until(reader: LineReader, stop_at: float):
    """读到 stop_at 为止，产出 (到达时刻, 音频长度秒, RMS)。"""
    while time.monotonic() < stop_at:
        for line in reader.lines():
            parsed = parse_line(line)
            chunk = parsed.value
            if chunk is None or not hasattr(chunk, "pcm"):
                continue
            yield time.monotonic(), len(chunk.pcm) / 2 / chunk.rate, chunk_rms(chunk.pcm)


def measure_once(reader: LineReader, click: Path, lead_in: float, listen: float):
    """打一次脉冲，返回 (峰值到达时刻秒, 峰值 RMS, 本底 RMS, 样本序列)。

    刻意不设固定阈值来判"听到没有"：阈值需要本底稳定，而实测里本底会随环境漂移，
    于是同一段录音有时报 971 ms、有时报"没听到"。改成直接报整段里最强的那一点出现在
    什么时刻，再由调用方看多次之间是否一致。这样输出不会因为阈值选错而变成噪声。
    """
    time.sleep(lead_in)
    baseline = []
    for _, _, rms in collect_until(reader, time.monotonic() + 0.35):
        baseline.append(rms)
    floor = sorted(baseline)[len(baseline) // 2] if baseline else 0.0

    started = time.monotonic()
    subprocess.Popen(
        ["afplay", str(click)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    series = []
    for arrival, _, rms in collect_until(reader, started + listen):
        series.append((arrival - started, rms))
    if not series:
        return None, 0.0, floor, []
    peak_offset, peak = max(series, key=lambda item: item[1])
    return peak_offset, peak, floor, series


def report(index: int, offset: float, peak: float, floor: float, series) -> float:
    ranked = sorted(series, key=lambda item: item[1], reverse=True)[:3]
    tops = "、".join(f"{item[0] * 1000:.0f} ms/{item[1]:.0f}" for item in ranked)
    print(f"  第 {index + 1} 次：最强点 {offset * 1000:.0f} ms"
          f"（RMS {peak:.0f}，本底 {floor:.0f}，信噪比 {peak / max(floor, 1.0):.1f}×）")
    print(f"            前三个波峰：{tops}")
    return offset


def measure_via_relay(reader: LineReader, source: "RelaySource", args) -> list[float | None]:
    """走真正的中继播放器测一轮。基线先收，再等脉冲进队，之后只统计它之后的部分。"""
    click_pcm = build_click_pcm(RELAY_RATE)
    results: list[float | None] = []
    for index in range(args.repeats):
        source.click_at = None
        worker = threading.Thread(
            target=source.stream,
            args=(click_pcm, args.lead_in, args.lead_in + args.listen + 0.2),
            daemon=True,
        )
        worker.start()
        baseline = []
        for _, _, rms in collect_until(reader, time.monotonic() + 0.4):
            baseline.append(rms)
        floor = sorted(baseline)[len(baseline) // 2] if baseline else 0.0
        while source.click_at is None:
            time.sleep(0.002)
        started = source.click_at
        series = [
            (arrival - started, rms)
            for arrival, _, rms in collect_until(reader, started + args.listen)
        ]
        worker.join(timeout=2)
        if not series:
            print(f"  第 {index + 1} 次：这段时间一个音频块都没收到")
            results.append(None)
            continue
        offset, peak = max(series, key=lambda item: item[1])
        results.append(report(index, offset, peak, floor, series))
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="测 Mac → AirPods → S3 麦克风 → USB 的往返延迟")
    parser.add_argument("--port", required=True, help="串口，例如 /dev/cu.usbmodem101")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=0.05,
                        help="串口读超时（秒）。越小越灵敏，代价是空转")
    parser.add_argument("--source", choices=("relay", "afplay"), default="relay",
                        help="relay=走真正的中继播放器（默认，蓝牙链路是热的）；"
                             "afplay=每次冷启动一个播放器，用来对比蓝牙唤醒代价")
    parser.add_argument("--player", choices=("sink", "ffplay"), default="sink",
                        help="中继播放端：sink=新的（报排队深度、按上限丢弃）；"
                             "ffplay=旧的，用于 A/B 对比")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--listen", type=float, default=1.2, help="每轮听多久（秒）")
    parser.add_argument("--lead-in", type=float, default=0.7, help="每轮开始前的静默（秒）")
    args = parser.parse_args(argv)

    try:
        import serial  # noqa: PLC0415
    except ImportError:
        print("缺少 pyserial。安装：pip install pyserial", file=sys.stderr)
        return 3

    workdir = Path(tempfile.mkdtemp(prefix="loop-latency-"))
    click = workdir / "click.wav"
    samples = make_click(click)
    print(f"脉冲：{CLICK_HZ} Hz {CLICK_MS} ms（{samples} 点），文件 {click}")

    connection = serial.Serial(port=args.port, baudrate=args.baud, timeout=args.timeout)
    if args.port.startswith(("/dev/cu.usbmodem", "/dev/tty.usbmodem")):
        # 内置 USB Serial/JTAG 必须置位 DTR，否则主机一个字节都收不到（项目实测过）。
        connection.dtr = True
    reader = LineReader(connection)

    try:
        connection.reset_input_buffer()
        connection.write(b"rec start\r\n")
        if not wait_for_capture(reader, 5.0):
            print("设备没有进入录音态，先确认固件在跑、串口没被别的进程占用", file=sys.stderr)
            return 2
        print("设备已进入录音态，开始测量…")

        if args.source == "relay":
            source = RelaySource(RELAY_RATE, player=args.player)
            source.start()
            try:
                results = measure_via_relay(reader, source, args)
            finally:
                source.stop()
        else:
            results = []
            for index in range(args.repeats):
                onset, peak, floor, series = measure_once(
                    reader, click, args.lead_in, args.listen
                )
                if onset is None:
                    print(f"  第 {index + 1} 次：这段时间一个音频块都没收到")
                    results.append(None)
                    continue
                results.append(report(index, onset, peak, floor, series))

        good = [value for value in results if value is not None]
        if good:
            print("")
            print(f"往返延迟：最小 {min(good) * 1000:.0f} ms、"
                  f"中位 {sorted(good)[len(good) // 2] * 1000:.0f} ms")
            if args.source == "relay":
                print("这条链路 = 中继队列 + 播放器缓冲 + AirPods 输出 + 空气 + S3 采集 + USB")
                print("它等于「我说话 → 在 AirPods 里听到」的延迟：两者各含一次麦克风与一次放音")
            else:
                print("这条链路 = afplay 启动 + AirPods 输出 + 空气 + S3 采集 + USB + 主机读取")
        else:
            print("三次都没有测到脉冲", file=sys.stderr)
            return 1
        return 0
    finally:
        try:
            connection.write(b"rec stop\r\n")
            time.sleep(0.3)
        except Exception:  # noqa: BLE001 - 收尾失败不该盖住测量结果
            pass
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
