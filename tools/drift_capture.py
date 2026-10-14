#!/usr/bin/env python3
"""Collect the latency report and command stream from the device for a fixed duration.

Used to answer the drift question in docs/edge-feasibility.md: a control loop that is
stable for thirty seconds and wanders after an hour is a different claim from one that is
stable all day, and only a long capture distinguishes them.

Writes JSONL, one line per received JSON object. Lines are flushed as they arrive so an
interrupted run still leaves usable data.

Usage
-----
    python3 tools/drift_capture.py --port /dev/cu.usbmodem1101 --seconds 3600 \\
        --out 记录/drift.jsonl

Summary
-------
    python3 tools/drift_capture.py --summarise 记录/drift.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


def collect(port: str, baud: int, seconds: float, out: Path) -> int:
    try:
        import serial  # noqa: PLC0415
    except ImportError:
        # The system python3 on macOS usually has no pyserial while the interpreter used by
        # tools/monitor.sh does. Say which interpreter is running so the fix is obvious
        # instead of leaving the reader to guess.
        print(
            f"缺少 pyserial，当前解释器是 {sys.executable}。\n"
            "可用的解释器（带 pyserial）通常是 tools/monitor.sh 里那一个：\n"
            "  /Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/"
            "archive-v0/LSKE5uaT0ygsXUNE/bin/python\n"
            "用 CORES3_PYTHON 指定，或直接用该解释器运行本脚本。",
            file=sys.stderr,
        )
        return 3

    out.parent.mkdir(parents=True, exist_ok=True)
    connection = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    # Do not touch DTR/RTS: opening the port must not reset a running device.
    connection.dtr = False
    connection.rts = False

    kept = 0
    deadline = time.monotonic() + seconds
    print(f"采集 {seconds:.0f}s -> {out}", flush=True)
    try:
        with out.open("w", encoding="utf-8") as handle:
            while time.monotonic() < deadline:
                try:
                    raw = connection.readline()
                except Exception as exc:  # a transient USB error must not end the run
                    print(f"读取异常，继续：{exc}", file=sys.stderr, flush=True)
                    continue
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith('{"type":"latency"') or line.startswith(
                    '{"type":"earbud_command"'
                ):
                    handle.write(line + "\n")
                    handle.flush()
                    kept += 1
    except KeyboardInterrupt:
        print("被中断", file=sys.stderr)
    finally:
        connection.close()
    print(f"完成，写入 {kept} 行", flush=True)
    return 0


def summarise(path: Path) -> int:
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 2

    reports = []
    commands = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "latency":
            reports.append(payload)
        elif payload.get("type") == "earbud_command":
            commands.append(payload)

    if not reports and not commands:
        print("没有可汇总的数据", file=sys.stderr)
        return 1

    print(f"=== {path} ===")
    if commands:
        stamps = [c["timestamp_ms"] for c in commands]
        gaps = sorted(stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1))
        span = (stamps[-1] - stamps[0]) / 1000.0
        print(
            f"命令帧 {len(commands)} 条 / {span:.0f}s；间隔 "
            f"p50={gaps[len(gaps) // 2]} ms  p95={gaps[int(len(gaps) * 0.95)]} ms  "
            f"max={gaps[-1]} ms"
        )

    if reports:
        print(f"\n延迟报告 {len(reports)} 份")
        # Drift is the question, so compare the first and last quarters rather than
        # averaging the whole run: an average hides exactly the thing being looked for.
        quarter = max(len(reports) // 4, 1)
        stages: dict[str, list[float]] = {}
        for report in reports:
            for stage in report["stages"]:
                if stage["n"]:
                    stages.setdefault(stage["stage"], []).append(stage["p50_ms"])

        print(f"{'stage':<10} {'first 25% p50':>14} {'last 25% p50':>14} {'drift':>10}")
        for name, values in stages.items():
            head = statistics.median(values[:quarter])
            tail = statistics.median(values[-quarter:])
            print(f"{name:<10} {head:>14.3f} {tail:>14.3f} {tail - head:>+10.3f}")

        overhead = [r["overhead_mean_us"] for r in reports if "overhead_mean_us" in r]
        if overhead:
            print(
                f"\n打点开销 mean={statistics.median(overhead):.2f} us  "
                f"max={max(overhead):.2f} us"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="设备延迟长窗口采集与汇总")
    parser.add_argument("--port", default="/dev/cu.usbmodem1101")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=3600.0)
    parser.add_argument("--out", default="记录/drift.jsonl")
    parser.add_argument("--summarise", default=None, help="只汇总已有的 JSONL")
    parser.add_argument(
        "--python",
        default=None,
        help="带 pyserial 的解释器路径；省略时用当前解释器（兼容旧调用方式）",
    )
    args = parser.parse_args()

    if args.summarise:
        return summarise(Path(args.summarise))
    return collect(args.port, args.baud, args.seconds, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
