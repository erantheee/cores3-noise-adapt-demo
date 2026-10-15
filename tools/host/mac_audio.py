"""Read the real macOS default audio output for the CoreS3 route page."""

from __future__ import annotations

import platform
import subprocess


def parse_default_output(report: str) -> str | None:
    current: str | None = None
    for raw_line in report.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if line.startswith("        ") and not line.startswith("          ") and stripped.endswith(":"):
            current = stripped[:-1]
        elif stripped in {"Default Output Device: Yes", "Default System Output Device: Yes"}:
            return current
    return None


def default_output() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/sbin/system_profiler", "SPAudioDataType"],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_default_output(result.stdout)


#: 中继期望声音最终到达的设备。
#:
#: 只用来判断"声音有没有送到该去的地方"，**不用来切换输出**：默认输出是用户的系统设置，
#: 由蓝牙连接的实际情况决定，工具擅自改它会让"没声音"变成更难查的问题。
EXPECTED_TARGET = "AirPods Pro"


def route_command(device: str | None, target: str = EXPECTED_TARGET) -> bytes:
    safe_target = " ".join(target.replace("\r", " ").replace("\n", " ").split())[:47]
    state = "active" if device == target else "inactive"
    return f"route {state} {safe_target}\r\n".encode("utf-8")
