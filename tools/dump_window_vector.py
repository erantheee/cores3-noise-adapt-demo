#!/usr/bin/env python3
"""调试：串口发 `vector`，把端侧满窗 92 维特征和对应帧一起存成 JSONL。

用于核对固件 `context_window` 与训练侧 `features.py` 是否同口径——金标准只覆盖
录下来的样例，不能证明运行时的每一维都对得上。会独占串口，先停上位机。

    python3 tools/dump_window_vector.py --port /dev/cu.usbmodem101 --seconds 8
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import serial


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/cu.usbmodem101")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--out", type=Path, default=Path("/tmp/window_vectors.jsonl"))
    args = parser.parse_args()

    deadline = time.monotonic() + args.seconds
    next_request = 0.0
    with serial.Serial(args.port, args.baud, timeout=0.2) as port, \
            args.out.open("w", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_request:
                port.write(b"vector\n")
                next_request = time.monotonic() + 0.5
            line = port.readline().decode("utf-8", "replace").strip()
            if line.startswith("{"):
                handle.write(line + "\n")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
