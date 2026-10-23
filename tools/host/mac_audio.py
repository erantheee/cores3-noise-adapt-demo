"""Read the real macOS default audio output for the CoreS3 route page."""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Earphone:
    """一副可识别的耳机：设备名子串 + S3 路由页显示名。"""

    match: str
    label: str


#: 当前版本同时支持的耳机（2026-10-21 起，D35/D36）：
#: OWS 耳夹（Xiaomi 耳夹式耳机）与 TWS 对照（AirPods Pro）。
#:
#: macOS 报出来的名字可能带型号后缀、大小写也不统一，所以按**大小写不敏感子串**匹配，
#: 列表顺序即优先级（默认两条不重叠）。加耳机/换耳机用 `--earphone MATCH=LABEL`，
#: 不改代码。
#:
#: 显示名 **必须是 ASCII**：`lv_font_ui_16` 是 115 字子集，别赌「夹」「式」这类字存在
#: （见 `main/demo_ui.cpp` 的字库说明与 `main/lv_font_ui_16.c` 的生成参数）。
DEFAULT_EARPHONES: tuple[Earphone, ...] = (
    Earphone("Xiaomi", "Xiaomi OWS"),
    Earphone("AirPods", "AirPods Pro"),
)

#: 当前默认输出不是任何已知耳机时，S3 路由页显示的名字（ASCII）。
FALLBACK_LABEL = "Earphone"


def match_earphone(
    device: str | None, earphones: Sequence[Earphone] = DEFAULT_EARPHONES
) -> Earphone | None:
    """返回设备名命中的耳机；未命中（含没有默认输出）返回 None。"""
    if not device:
        return None
    name = device.lower()
    for earphone in earphones:
        if earphone.match and earphone.match.lower() in name:
            return earphone
    return None


def route_command(
    device: str | None,
    earphones: Sequence[Earphone] = DEFAULT_EARPHONES,
    fallback_label: str = FALLBACK_LABEL,
) -> bytes:
    """生成 S3 路由页的回传命令。

    `device` 是 Mac 当前默认输出；命中 `earphones` 就报 active 并显示那副耳机的短名，
    否则报 inactive 并显示 `fallback_label`。显示名必须与设备字库兼容，否则上屏是方框。
    """
    earphone = match_earphone(device, earphones)
    label = earphone.label if earphone else fallback_label
    safe_label = " ".join(label.replace("\r", " ").replace("\n", " ").split())[:47]
    state = "active" if earphone else "inactive"
    return f"route {state} {safe_label}\r\n".encode("utf-8")
