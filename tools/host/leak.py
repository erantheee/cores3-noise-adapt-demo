"""把 Mac 自己放的歌从 S3 听到的 24 段环境谱里估出来（阶段二/B2，2026-10-30）。

## 为什么需要

掩蔽矩阵（D56）比较的是"S3 报的环境谱"和"歌曲的内容谱"。但 S3 贴在耳边/肩上时，
**自己放出来的歌也会进麦克风**：现场实测肩上位置有 25–33% 的帧被情境模型判成 music，
漏音还会偶尔触发 1 s 事件层的 speech。结果就是矩阵把"歌自己的频段"当成"噪声占优"，
一边补一边更明显——一个正反馈。

## 口径（必须写清楚，否则很容易越界）

* 只比较**谱形**，不比较绝对声压：`ambient[i] − max(ambient)` 与
  `content[i] − max(content)` 都在相对 dB 域。
* 漏音假设：环境谱 ≈ 歌的谱形 × 一个未知整体电平。对"歌有内容"的频段取
  `ambient[i] − content[i]` 的中位数，就是那个整体电平。
* 残差 = 功率域相减，**最多减 12 dB**（`MAX_SUBTRACT_DB`）。宁可少减，也不能把真实的
  环境噪声一起减掉——减错了会直接让掩蔽变小，而这是用户听得见的东西。
* 一致性门：`ambient − content` 在各频段的离散度（中位绝对偏差）超过 6 dB 就认为
  "这不是这首歌的漏音"（房间本底、别处的声音、或内容谱已经过期），**原样放行**。
* 默认只观测（`--leak-residual observe`）：没有真机 A/B 之前不改变调音动作，
  和九分类同一条纪律——先证明有用，再进决策。

纯函数、无 IO，主机单测见 `tools/host/tests/test_host.py`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: 与固件/矩阵一致的段数。
BAND_COUNT = 24
#: 只有歌曲在该段确有内容（相对谱形高于这个值）才拿它估漏音。
MIN_CONTENT_DB = -24.0
#: 单段最多减多少 dB。减多了会把真实环境噪声一起吃掉。
MAX_SUBTRACT_DB = 12.0
#: `ambient − content` 的离散度超过它就不认为是漏音。
MAX_SPREAD_DB = 6.0
#: 低于这个置信度不应用（只报遥测）。
MIN_CONFIDENCE = 0.6
#: 至少用多少个"有内容的频段"做估计，否则中位数没有意义。
MIN_BANDS = 6
#: 歌曲谱形自身的动态范围太小（近似平谱）时，环境谱"像不像这首歌"根本无从判断——
#: 一条平谱可以解释成任何歌的漏音。这种情况下直接放行，不冒险。
MIN_SHAPE_RANGE_DB = 8.0
#: 固件用 -120 dBFS 表示没有有效能量。
NO_DATA_DBFS = -120.0


@dataclass(frozen=True)
class LeakEstimate:
    """一次漏音估计的结果。`bands` 永远是可用的 24 段谱（没应用时就是原值）。"""

    bands: list[float]
    applied: bool
    leak_level_db: float | None = None
    confidence: float = 0.0
    spread_db: float | None = None
    #: 为什么没有应用：`disabled` / `invalid` / `few_bands` / `low_confidence`；应用时为空。
    reason: str = ""


def _valid_spectrum(values, length: int = BAND_COUNT) -> bool:
    return (
        isinstance(values, (list, tuple))
        and len(values) == length
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in values
        )
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def estimate_leak(ambient_dbfs, content_shape_db, *, enabled: bool = True) -> LeakEstimate:
    """估计并可选地扣掉 Mac 侧歌曲漏音。永远返回 24 段可用谱。"""
    ambient = [float(value) for value in ambient_dbfs] if _valid_spectrum(ambient_dbfs) else []
    content = [float(value) for value in content_shape_db] if _valid_spectrum(content_shape_db) else []
    if not ambient or not content:
        return LeakEstimate(bands=ambient or [], applied=False, reason="invalid")
    if not enabled:
        return LeakEstimate(bands=ambient, applied=False, reason="disabled")

    active = [
        index
        for index in range(BAND_COUNT)
        if content[index] > MIN_CONTENT_DB and ambient[index] > NO_DATA_DBFS
    ]
    if len(active) < MIN_BANDS:
        return LeakEstimate(bands=ambient, applied=False, reason="few_bands")
    shape_range = max(content[index] for index in active) - min(
        content[index] for index in active
    )
    if shape_range < MIN_SHAPE_RANGE_DB:
        return LeakEstimate(bands=ambient, applied=False, reason="flat_content")

    # 谱形是相对的（各自最强段为 0 dB），所以差值的中位数就是一个整体偏移：
    # ambient ≈ content + offset，offset 即"这首歌打到麦克风上的整体电平"。
    differences = [ambient[index] - content[index] for index in active]
    offset = _median(differences)
    deviations = [abs(value - offset) for value in differences]
    spread = _median(deviations)
    confidence = max(0.0, min(1.0, 1.0 - spread / MAX_SPREAD_DB))
    if confidence < MIN_CONFIDENCE:
        return LeakEstimate(
            bands=ambient,
            applied=False,
            leak_level_db=offset,
            confidence=confidence,
            spread_db=spread,
            reason="low_confidence",
        )

    # 功率域相减：dB 域直接减会把小量算成负数，功率域才是物理上正确的做法。
    # 减完剩不到 MAX_SUBTRACT_DB 的段直接压到地板，避免 log 出 -inf。
    floor_ratio = 10.0 ** (-MAX_SUBTRACT_DB / 10.0)
    residual: list[float] = []
    for index in range(BAND_COUNT):
        level = ambient[index]
        if index not in active:
            # 歌在这一段没有内容：没有可解释的漏音，原样保留（可能是真实环境声）。
            residual.append(level)
            continue
        ambient_power = 10.0 ** (level / 10.0)
        leak_power = 10.0 ** ((offset + content[index]) / 10.0)
        remaining = ambient_power - leak_power
        if remaining <= ambient_power * floor_ratio:
            residual.append(level - MAX_SUBTRACT_DB)
        else:
            residual.append(10.0 * math.log10(remaining))
    return LeakEstimate(
        bands=residual,
        applied=True,
        leak_level_db=offset,
        confidence=confidence,
        spread_db=spread,
        reason="",
    )
