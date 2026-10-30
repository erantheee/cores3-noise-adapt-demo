"""内容类型 → 24 段保护（Layer 1，2026-10-30）。

用户口径："理论上只是多了一层分轨和歌曲类型判断，给轨道做了一定的保护，不至于
不同的歌曲用同一套方案来调节……一样还是回归 EQ。"

所以这一层**只做保护、不替代基础 EQ**：在 S3 24 段处方上叠 ±1～2 dB 的段偏移，
并按类型给一个整体剂量系数（0.85～1.0）。基础 EQ 的可听幅度完全保留；
类型错了也只是 ±1.5 dB 的偏移，不会把曲子调坏。频段分组与 `tools/host/media.py`
的 BAND_GROUPS 一致（0–7 低 / 8–15 中 / 16–23 高）。

纯函数、无 IO，主机单测见 `tools/host/tests/test_host.py::test_eq_profile`。
"""

from __future__ import annotations

BAND_COUNT = 24

#: kind → (24 段偏移 dB, 整体剂量系数, 面板说明)。偏移全部 ≤ |2| dB。
KIND_PROFILES: dict[str, tuple[list[float], float, str]] = {
    "bass_heavy": (
        [-1.5] * 5 + [-0.5] * 3 + [0.0] * 16,
        1.0,
        "低频偏重：低频 0–4 段 −1.5 dB、中低频 −0.5 dB（防轰，presence 不受影响）",
    ),
    "dense": (
        [0.0] * 16 + [-1.5] * 4 + [-1.0] * 4,
        1.0,
        "高频密集：6 kHz 以上收 1–1.5 dB（防刺，不动中低频）",
    ),
    "pop_vocal": (
        [0.0] * 5 + [-0.5] * 3 + [0.0] * 8 + [0.5] * 8,
        1.0,
        "人声流行向：中低频 −0.5 dB 去糊、超高频 +0.5 dB（presence 保持原样）",
    ),
    "rhythmic": (
        [0.5] * 5 + [0.0] * 19,
        0.85,
        "节奏型：低频 +0.5 dB、整体剂量 ×0.85（强瞬态下避免过冲）",
    ),
    "ambient": (
        [0.0] * 16 + [-0.5] * 8,
        1.0,
        "器乐/氛围：高频 −0.5 dB（少动高频，保持安静感）",
    ),
    "bright": (
        [0.0] * 16 + [-1.0] * 8,
        1.0,
        "偏亮：高频 −1.0 dB（防再叠高频）",
    ),
    "pop": (
        [0.0] * 24,
        1.0,
        "综合流行：不做偏移",
    ),
}

#: 未知/无内容时不调整。空说明 = 面板不显示这一行。
_DEFAULT = ([0.0] * BAND_COUNT, 1.0, "")


def profile_for(kind: str | None) -> tuple[list[float], float, str]:
    """kind → (24 段偏移, 剂量系数, 面板说明)。未知类型返回全 0 / 1.0 / 空说明。"""
    offsets, scale, summary = KIND_PROFILES.get(str(kind or ""), _DEFAULT)
    return list(offsets), float(scale), summary


def protected_bands(base_bands, kind: str | None) -> list[float]:
    """基础 24 段 + 类型保护偏移 → 实际下发曲线。

    * 偏移在 dB 域相加，再乘剂量系数；
    * 夹回 S3 处方的合法范围 [-4, +4] dB；
    * 输入不足 24 段时按原样返回（缺段不猜）。
    """
    if not isinstance(base_bands, (list, tuple)) or len(base_bands) != BAND_COUNT:
        return list(base_bands)
    offsets, scale, _ = profile_for(kind)
    result: list[float] = []
    for value, offset in zip(base_bands, offsets):
        try:
            number = float(value)
        except (TypeError, ValueError):
            result.append(0.0)
            continue
        result.append(max(-4.0, min(4.0, (number + offset) * scale)))
    return result
