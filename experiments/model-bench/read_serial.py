#!/usr/bin/env python3
"""读 CoreS3 串口输出 N 秒，逐行打印。

DTR/RTS 必须置位：本机实测 dtr=False 时端口能开、能写，但永远读不到字节
（cores3-noise-adapt-demo 的 plan-status 里记过这条）。用内置 USB-Serial/JTAG 时，
置位这两个调制解调器线不会复位芯片。

需要 pyserial：用 IDF 的 python 环境运行
    /Users/eranthe/.espressif/python_env/idf5.4_py3.12_env/bin/python read_serial.py --port /dev/cu.usbmodem101 --seconds 8
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("缺少 pyserial：请用 IDF 的 python 环境运行本脚本")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=8.0)
    args = ap.parse_args()

    deadline = time.time() + args.seconds
    with serial.Serial(port=args.port, baudrate=args.baud, timeout=0.2) as ser:
        ser.dtr = True
        ser.rts = True
        while time.time() < deadline:
            line = ser.readline()
            if not line:
                continue
            print(line.decode("utf-8", errors="replace").rstrip())


if __name__ == "__main__":
    main()
