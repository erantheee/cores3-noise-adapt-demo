"""「当前媒体 + 歌曲类型（估计）+ 调音思路」的判据层（2026-10-30）。

用户口径（原话）：**这个板块负责显示现在听到的媒体（比如 QQ 音乐）、歌曲的类型（比如
pop）、调音思路。** 所以判断都放在 host，面板只负责照实显示——和「面板是渲染不是判断」
同一条纪律。

## 三件事各自能拿到什么、拿不到什么

1. **媒体源**：能确定。执行端是 systemeq tap 就是 QQ 音乐；只有本地播放器在播就是本机
   播放器；都没有就是没有媒体。**曲目名/歌手读不到**：QQ 音乐在 macOS 上没有可用的
   AppleScript 曲目接口，MediaRemote 的 Now Playing 探测也拿不到（2026-10-30 实测回调
   不返回）。所以曲目一栏如实写"不可读"，不猜。
2. **歌曲类型**：**没有流派元数据**，只能从内容谱形/瞬态/高频密度做**粗分类估计**。
   标签一律带「（估计）」，并附依据数字；它不是 genre 识别模型，不能当成事实。
3. **调音思路**：全部来自执行端实测（掩蔽段、峰值、剂量、强度、瞬态风险、输出余量、
   漏音残差状态），一行行说明"在做什么、被什么限制"。

纯函数、无 IO，主机单测见 `tools/host/tests/test_host.py::test_media_card`。
"""

from __future__ import annotations

import math

from .eq_profile import profile_for

#: 24 段中心频率分组（与固件/systemeq 的 log 段一致）：只用于把"哪几段在动"讲成人话。
BAND_GROUPS = (
    ("超低频 70–140 Hz", range(0, 2)),
    ("低频 170–350 Hz", range(2, 5)),
    ("中低频 430–900 Hz", range(5, 8)),
    ("中频 1–2.2 kHz", range(8, 12)),
    ("中高频 2.7–5 kHz", range(12, 16)),
    ("高频 6–11 kHz", range(16, 20)),
    ("超高频 13 kHz+", range(20, 24)),
)

#: 没有内容时的兜底（RMS 低于它基本等于没在出声）。
SILENT_RMS = 0.02


def _num(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # 过滤 NaN


def _share(shape: list[float], indices: range) -> float:
    """把相对 dB 谱形按线性功率归一化到 0…1，返回该组占全谱的比例。"""
    total = 0.0
    part = 0.0
    for index, value in enumerate(shape):
        power = 10.0 ** (float(value) / 10.0)
        total += power
        if index in indices:
            part += power
    return part / total if total > 0 else 0.0


def _band_ranges(indices: list[int]) -> list[str]:
    """把命中的段号翻成"哪几个频段在动"，不列 24 个数字。"""
    names: list[str] = []
    hit = set(indices)
    for name, span in BAND_GROUPS:
        if hit & set(span):
            names.append(name)
    return names


def estimate_content_kind(systemeq: dict) -> dict:
    """从内容谱形/瞬态/高频密度做**粗分类估计**。返回 kind/text/confidence/basis。"""
    shape = systemeq.get("content_band_shape_db") or []
    analyses = _num(systemeq.get("content_analyses"), 0) or 0
    rms = _num(systemeq.get("content_window_rms"), 0.0) or 0.0
    harshness = _num(systemeq.get("content_harshness"), 0.0) or 0.0
    transient = _num(systemeq.get("content_transient_risk"), 0.0) or 0.0
    if len(shape) != 24 or analyses <= 0 or rms < SILENT_RMS:
        return {
            "kind": "unknown",
            "text": "无内容（还没分析到歌）",
            "confidence": 0.0,
            "basis": f"RMS {rms:.3f} · 分析 {int(analyses)} 次",
        }

    # 三段必须覆盖全部 24 段（0–7 低 / 8–15 中 / 16–23 高），否则占比不归一，
    # 低频向的歌会被误判成"中频没有内容"。2026-10-30 真机第一次接上时踩到过。
    low = _share(shape, range(0, 8))
    mid = _share(shape, range(8, 16))
    high = _share(shape, range(16, 24))
    basis = (
        f"低/中/高能量 {low:.0%}/{mid:.0%}/{high:.0%} · "
        f"高频密度 {harshness:.0%} · 瞬态 {transient:.0%}"
    )

    # 规则按"最能区分听感处理方式"的顺序排，不追求流派学名：
    # 低频+强瞬态＝节奏型；高频密度高＝密集/电子；中频主导且不刺＝人声流行。
    # 标签不冒充流派名（没有 genre 元数据）：写"听起来偏什么 + 调音会怎么对待"。
    # 用户口径是"歌曲的类型，比如 pop"，所以最后的兜底就叫"综合流行"。
    if transient >= 0.45 and low >= 0.30:
        return {"kind": "rhythmic", "text": "节奏型 · 低频+强瞬态（估计）",
                "confidence": 0.65, "basis": basis}
    if harshness >= 0.55:
        return {"kind": "dense", "text": "高频密集 · 收高频更积极（估计）",
                "confidence": 0.6, "basis": basis}
    if low >= 0.85 and transient < 0.35:
        return {"kind": "bass_heavy", "text": "低频偏重 · 防轰（估计）",
                "confidence": 0.6, "basis": basis}
    if mid >= 0.35 and transient < 0.40 and high >= 0.03:
        return {"kind": "pop_vocal", "text": "中频突出 · 人声流行向（估计）",
                "confidence": 0.6, "basis": basis}
    if high >= 0.30:
        return {"kind": "bright", "text": "偏亮 · 高频多（估计）",
                "confidence": 0.5, "basis": basis}
    if high < 0.10 and transient < 0.30:
        return {"kind": "ambient", "text": "低频/中频为主 · 器乐或氛围（估计）",
                "confidence": 0.55, "basis": basis}
    return {"kind": "pop", "text": "综合流行 · 无明显偏向（估计）",
            "confidence": 0.45, "basis": basis}


def tuning_lines(systemeq: dict, frame: dict | None, leak: dict | None) -> list[str]:
    """调音思路：只写实测到的动作与被哪道门限制。"""
    if not systemeq or systemeq.get("available") is False:
        return ["执行端没在跑：现在听到的是原声。"]

    lines: list[str] = []
    if systemeq.get("bypass"):
        return ["已直通（关闭EQ）：听到的是原声，不做任何调音。"]
    mode = (frame or {}).get("mode")
    if mode == "FLAT":
        return ["S3 报安静/对话：EQ 平直，掩蔽暂停（对话优先）。"]

    if not systemeq.get("masking_matrix_enabled"):
        lines.append("掩蔽矩阵待环境谱：这一拍只有基础 EQ，没有逐段补偿。")
        return lines

    bands = systemeq.get("masking_bands") or []
    boosts = [i for i, value in enumerate(bands) if _num(value, 0.0) > 0.3]
    cuts = [i for i, value in enumerate(bands) if _num(value, 0.0) < -0.3]
    peak = _num(systemeq.get("masking_peak_db"), 0.0) or 0.0
    dose = _num(systemeq.get("masking_dose"), 1.0) or 1.0
    exposure = _num(systemeq.get("masking_exposure"), 0.0) or 0.0
    transient = _num(systemeq.get("content_transient_risk"), 0.0) or 0.0
    headroom = _num(systemeq.get("output_headroom_gain"), 1.0) or 1.0
    clip = _num(systemeq.get("out_clip_frames"), 0) or 0
    if boosts or cuts:
        parts = []
        if boosts:
            ranges = _band_ranges(boosts)
            where = "、".join(ranges[:3]) + (f" 等 {len(ranges)} 组" if len(ranges) > 3 else "")
            parts.append(f"抬 {len(boosts)} 段（{where}，目标峰值 {peak:.1f} dB）")
        if cuts:
            cut_ranges = _band_ranges(cuts)
            lowest = min(_num(bands[i], 0.0) for i in cuts)
            where = "、".join(cut_ranges[:3]) + (f" 等 {len(cut_ranges)} 组" if len(cut_ranges) > 3 else "")
            parts.append(f"让位 {len(cuts)} 段（{where}，最低 {lowest:.1f} dB）")
        lines.append(
            "舒适层（内容 × 环境补偿）：" + "；".join(parts) + f"（剂量 {dose:.1f}×）。"
        )
    else:
        lines.append("舒适层：这一拍没有「噪声压过歌曲」的频段，不做补偿。")
    gate = "正增益被收缩" if transient >= 0.15 else "未触发收缩"
    lines.append(f"遮蔽强度 {exposure:.0%}；内容瞬态 {transient:.0%} → {gate}。")
    headroom_db = 20.0 * math.log10(headroom) if headroom > 0 else 0.0
    lines.append(
        f"输出余量 {headroom_db:+.1f} dB（防止抬完削波）；自启动累计削波 {int(clip)} 帧"
        "（不增长＝没在削）。"
    )
    # 2026-10-30 用户口径：S3 24 段处方 × 深度 = 环境自适应 EQ（舒适层 A）的可听主体，
    # 而且它内部已经包含 S3 固件的事件驱动掩蔽包络（键盘 250 ms/2.6 s、EVI 1–4 kHz 拉满）。
    # 面板必须把它写出来，否则用户只看到补偿层的 1–3 dB，会以为"没调"。
    base_bands = [float(value) for value in ((frame or {}).get("tuning_bands") or [])]
    depth = _num(systemeq.get("depth"))
    kind = estimate_content_kind(systemeq).get("kind")
    _, _, protection = profile_for(kind)
    if protection:
        lines.append(f"类型保护：{protection}。")
    if len(base_bands) == 24 and depth:
        peak_db = max(base_bands) * depth
        low_db = min(base_bands) * depth
        lines.append(
            f"环境自适应 EQ（S3 24 段 × 深度 {depth:.1f}×，随噪声连续变化）："
            f"最高 {peak_db:+.1f} / 最低 {low_db:+.1f} dB。"
        )
    # 掩蔽层 B：事件驱动的增益包络，主体在 S3 固件侧完成，处方里已经带上。
    if (frame or {}).get("typing_active"):
        lines.append(
            "掩蔽：正在打字 → 键盘包络（起 250 ms / 落 2.6 s，作用 1–3.4 kHz，已在 S3 处方内）。"
        )
    elif (frame or {}).get("event") == "keyboard":
        lines.append("掩蔽：键盘事件 → S3 已在 1–3.4 kHz 加包络。")
    if (frame or {}).get("mode") == "EXTERNAL_VOICE_INTENT":
        lines.append("掩蔽：背景人声 → 1–4 kHz 拉满 + 全段 +1（S3 处方内）。")
    if leak:
        state = "已扣" if leak.get("applied") else "未扣"
        reason = {
            "ok": "估计成立", "disabled": "仅观测", "low_confidence": "环境谱不像漏音",
            "few_bands": "歌的频段太少", "flat_content": "歌频谱太平", "invalid": "缺谱",
        }.get(str(leak.get("reason") or ""), str(leak.get("reason") or "?"))
        lines.append(f"漏音残差：{state}（{reason}）。")
    return lines


def describe_media(*, systemeq: dict | None, player: dict | None,
                   frame: dict | None, leak: dict | None) -> dict:
    """面板「当前媒体」卡的完整内容。字段缺失一律如实写"不可读/没有"，不猜。"""
    systemeq = systemeq or {}
    player = player or {}
    qq_running = bool(systemeq) and systemeq.get("available") is not False
    player_playing = bool(player.get("playing"))

    if qq_running:
        execution = str(systemeq.get("execution") or "")
        source_text = "QQ 音乐（tap 执行端）"
        if execution == "applied":
            source_text += " · 正在出声"
        elif execution == "silent":
            source_text += " · 歌在播但实测无信号"
        elif execution == "bypass":
            source_text += " · 已直通"
        track = None
        track_note = "曲目信息不可读（QQ 音乐未暴露曲目接口）"
    elif player_playing:
        source_text = "Mac 本地播放器"
        track = player.get("track_title")
        track_note = "" if track else "曲目名不可读"
    else:
        source_text = "没有媒体在出声"
        track = None
        track_note = ""

    kind = estimate_content_kind(systemeq) if (qq_running or player_playing) else {
        "kind": "unknown", "text": "无内容", "confidence": 0.0, "basis": "",
    }
    return {
        "source": "qqmusic" if qq_running else ("player" if player_playing else "none"),
        "source_text": source_text,
        "track": track,
        "track_note": track_note,
        "content_kind": kind["kind"],
        "content_text": kind["text"],
        "content_confidence": kind["confidence"],
        "content_basis": kind["basis"],
        "tuning": tuning_lines(systemeq, frame, leak),
    }
