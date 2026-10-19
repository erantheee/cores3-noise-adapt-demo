"""分析 `tools/live_band_capture.py` 采到的 24 段谱 CSV，出统计与证据图。

输入是设备每一帧（10 Hz）的 24 个频段 dBFS，外加模型当时的输出。这个脚本回答四个问题：

1. 每类动作（安静 / 敲键盘 / 吹风）的频段形状差在哪；
2. 端侧 9 类模型当时把这些动作判成了什么；
3. 固件自己的语音判据有没有把它们当人声（`environment` / `conversation` / 媒体下压）；
4. 段首提示音 / 动作起始跳变（guard 窗）和**机械撞击**帧排除后，数字是否还成立。

**撞击帧默认剔除**（`--include-invalid` 可关闭）：设备被撞到桌子时会削波，固件把情境窗
清零，随后 2 s 都是 `context_valid=false`。这一条是 2026-10-19 会话里由用户确认的——不是
吹气导致的。撞击的宽带电平（−13 ~ −16 dBFS）比所有有效声音都高，且之后 2 s 没有模型输出。
把它们算进"风声"会平白抬高低频段、把 `unknown` 混进类别统计。

只做描述统计，不训练分类器；阈值与结论请写进同一目录的 report.md，不要从这里外推。

用法：
    <python with numpy+Pillow> tools/analyze_band_capture.py 记录/键盘风声/<时间>/live.csv
    ... --png 记录/键盘风声/<时间>/features.png
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BAND_CENTER_HZ = np.array([
    70.7, 141.4, 244.9, 346.4, 451.7, 566.8, 696.5, 841.7,
    996.8, 1171.3, 1370.0, 1595.0, 1854.7, 2154.1, 2503.3,
    2916.3, 3414.7, 4036.1, 4829.1, 5824.1, 7020.0, 8552.8,
    10677.1, 13638.2,
], dtype=np.float64)
PHASES = ["ready", "quiet", "speech", "keyboard", "pause", "wind", "knock", "done"]
PHASE_CN = {"ready": "准备", "quiet": "安静", "speech": "说话", "keyboard": "敲键盘",
            "pause": "停", "wind": "吹风", "knock": "敲桌", "done": "结束"}


def load(path: Path):
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"{path} 是空的")
    t = np.array([float(r["elapsed_s"]) for r in rows])
    phase = np.array([r["phase"] for r in rows])
    bands = np.array([[float(r[f"band_{i:02d}"]) for i in range(24)] for r in rows])
    valid = np.array([r["context_valid"] == "True" for r in rows])
    return rows, t, phase, bands, valid


def broadband_dbfs(bands: np.ndarray) -> np.ndarray:
    """24 段功率和（带限宽带近似，与 collect_from_device.py 同口径）。"""
    power = 10.0 ** (bands / 10.0)
    return 10.0 * np.log10(np.maximum(power.sum(axis=1), 1e-12))


def invalid_runs(valid: np.ndarray, t: np.ndarray):
    """`context_valid=false` 的连续段。撞击后固件清零情境窗，通常持续约 2 s。"""
    runs = []
    index = np.where(~valid)[0]
    if len(index) == 0:
        return runs
    start = index[0]
    previous = index[0]
    for current in index[1:]:
        if current - previous > 2:
            runs.append((start, previous))
            start = current
        previous = current
    runs.append((start, previous))
    return runs


def descriptors(bands: np.ndarray):
    power = 10.0 ** (bands / 10.0)
    centroid = (power * BAND_CENTER_HZ).sum(axis=1) / np.maximum(power.sum(axis=1), 1e-20)
    return {
        "lo": bands[:, :5].mean(axis=1),
        "mid": bands[:, 5:16].mean(axis=1),
        "hi": bands[:, 16:].mean(axis=1),
        "centroid": centroid,
    }


def guard_mask(t: np.ndarray, phase: np.ndarray, guard_s: float) -> np.ndarray:
    """每段开头 `guard_s` 秒置 False：换段提示音与动作起始跳变不属于该段。"""
    mask = np.ones(len(t), dtype=bool)
    last, start = phase[0], t[0]
    for i in range(len(t)):
        if phase[i] != last:
            last, start = phase[i], t[i]
        if t[i] - start < guard_s:
            mask[i] = False
    return mask


def report(rows, t, phase, bands, desc, guard: np.ndarray, valid: np.ndarray,
           include_invalid: bool = False) -> None:
    width = np.ones(len(t), dtype=bool) if include_invalid else valid
    note = "" if include_invalid else "，已剔除撞击/窗未满帧"
    print(f"每段频段形状（中位数；已剔除段首 guard 窗{note}）")
    print(f"{'段':<10}{'帧':>5}{'低频':>8}{'中频':>8}{'高频':>8}{'中-低':>8}{'质心Hz':>9}")
    for name in PHASES:
        m = (phase == name) & guard & width
        if not m.any():
            continue
        lo = np.median(desc["lo"][m]); mid = np.median(desc["mid"][m])
        hi = np.median(desc["hi"][m]); cent = np.median(desc["centroid"][m])
        print(f"{name:<10}{int(m.sum()):>5}{lo:>8.1f}{mid:>8.1f}{hi:>8.1f}"
              f"{mid - lo:>8.1f}{cent:>9.0f}")

    bb = broadband_dbfs(bands)
    runs = invalid_runs(valid, t)
    if runs:
        print("\n撞击/削波段（`context_valid=false`；固件清零情境窗后约 2 s 才会再满）")
        for start, end in runs:
            print(f"  t={t[start]:6.1f}–{t[end]:6.1f}s  {end - start + 1:3d} 帧  "
                  f"峰值宽带 {bb[start:end + 1].max():6.1f} dBFS  阶段 {phase[start]}")
        print(f"  有效帧峰值宽带 {bb[valid].max():6.1f} dBFS —— 撞击是整段里最响的事件")

    print("\n端侧 9 类模型输出（guard 窗内）")
    for name in ("quiet", "speech", "keyboard", "wind", "knock"):
        m = (phase == name) & guard
        if not m.any():
            continue
        counts = Counter(r["context"] for r, keep in zip(rows, m) if keep)
        total = sum(counts.values())
        text = "  ".join(f"{k} {v} ({100 * v / total:.0f}%)" for k, v in counts.most_common())
        print(f"  {name:<9}n={total:>3}  {text}")

    if any("event" in row and row.get("event") for row in rows):
        print("\n端侧事件判断（1 s 窗规则，guard 窗内；只统计 event_valid=true 的帧）")
        for name in ("quiet", "speech", "keyboard", "wind", "knock"):
            chosen = [row for row, keep in zip(rows, (phase == name) & guard)
                      if keep and row.get("event_valid") == "True"]
            if not chosen:
                continue
            counts = Counter(row["event"] for row in chosen)
            total = sum(counts.values())
            text = "  ".join(f"{key} {value} ({100 * value / total:.0f}%)"
                             for key, value in counts.most_common())
            artifacts = sum(1 for row, keep in zip(rows, (phase == name) & guard)
                            if keep and row.get("event") == "artifact")
            print(f"  {name:<9}n={total:>3}  {text}"
                  + (f"  · artifact {artifacts} 帧" if artifacts else ""))

    print("\n固件语音判据 / 媒体下压 在该段的占比")
    for name in ("quiet", "speech", "keyboard", "wind", "knock"):
        sel = [r for r in rows if r["phase"] == name]
        if not sel:
            continue
        env = Counter(r["environment"] for r in sel)
        conv = Counter(r["conversation"] for r in sel)
        media = np.array([float(r["media_percent"]) for r in sel])
        voice = env.get("VOICE", 0) + env.get("LOUD", 0)
        likely = conv.get("LIKELY", 0)
        print(f"  {name:<9}n={len(sel):>3}  人声/高噪声 {100 * voice / len(sel):5.1f}%"
              f"  conversation LIKELY {100 * likely / len(sel):5.1f}%"
              f"  媒体 p50 {np.median(media):.0f}%")


def _font(size: int):
    for path in ("/System/Library/Fonts/PingFang.ttc",
                 "/System/Library/Fonts/Hiragino Sans GB.ttc",
                 "/System/Library/Fonts/STHeiti Medium.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def smooth(x: np.ndarray, k: int = 9) -> np.ndarray:
    out = np.copy(x)
    half = k // 2
    for i in range(len(x)):
        out[i] = np.median(x[max(0, i - half): min(len(x), i + half + 1)])
    return out


def render(rows, t, phase, desc, out_path: Path, valid: np.ndarray) -> None:
    lo, mid = smooth(desc["lo"]), smooth(desc["mid"])
    cent = smooth(desc["centroid"])
    context = [r["context"] for r in rows]
    artifacts = invalid_runs(valid, t)

    width, height = 1500, 900
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    f13, f15, f17 = _font(13), _font(15), _font(17)
    margin_l, margin_r = 105, 45
    plot_w = width - margin_l - margin_r
    t0, t1 = t.min(), t.max()

    def x_of(tt):
        return margin_l + (tt - t0) / (t1 - t0) * plot_w

    phase_color = {"ready": "#f2f4f7", "quiet": "#e3ebf7", "keyboard": "#efe4f8",
                   "pause": "#faf6ea", "wind": "#ddf2e6", "done": "#f2f4f7"}

    def shade(top, bottom):
        start = 0
        for i in range(1, len(phase) + 1):
            if i == len(phase) or phase[i] != phase[start]:
                draw.rectangle([x_of(t[start]), top, max(x_of(t[i - 1]), x_of(t[start]) + 1), bottom],
                               fill=phase_color.get(phase[start], "white"))
                start = i

    band_top = 16
    shade(band_top, band_top + 30)
    start = 0
    for i in range(1, len(phase) + 1):
        if i == len(phase) or phase[i] != phase[start]:
            x0, x1 = x_of(t[start]), x_of(t[i - 1])
            if x1 - x0 > 34:
                draw.text(((x0 + x1) / 2, band_top + 15), PHASE_CN.get(phase[start], phase[start]),
                          font=f15, fill="#444444", anchor="mm")
            start = i

    for start, end in artifacts:
        x0, x1 = x_of(t[start]), x_of(t[end])
        draw.rectangle([x0, band_top, max(x1, x0 + 2), band_top + 30], outline="#c0392b", width=2)

    panels = [
        ("低频段平均电平 (dBFS)", lo, (-75, -45), "#0b8f5a", "吹风时低频段抬高"),
        ("中频段平均电平 (dBFS)", mid, (-82, -52), "#7a3fb0", "敲键盘时中频段抬高"),
        ("频谱质心 (Hz)", cent, (0, 1500), "#c0392b", "键盘质心高、风与安静低"),
    ]
    panel_h, gap, first_top = 170, 52, band_top + 76
    for index, (title, values, (ymin, ymax), color, note) in enumerate(panels):
        top = first_top + index * (panel_h + gap)
        shade(top, top + panel_h)
        for frac in (0.0, 0.5, 1.0):
            value = ymin + (ymax - ymin) * frac
            y = top + panel_h - frac * panel_h
            draw.line([margin_l, y, margin_l + plot_w, y], fill="#d5d5d5")
            draw.text((margin_l - 10, y), f"{value:g}", font=f13, fill="#666666", anchor="rm")
        ys = top + panel_h - (np.clip(values, ymin, ymax) - ymin) / (ymax - ymin) * panel_h
        draw.line([(x_of(t[i]), ys[i]) for i in range(len(t))], fill=color, width=2)
        draw.text((margin_l, top - 26), title, font=f17, fill=color)
        draw.text((margin_l + plot_w, top - 26), note, font=f13, fill="#888888", anchor="ra")

    strip_top = first_top + 3 * (panel_h + gap) + 4
    # 撞击边界贯穿三条轨（细虚线）；标签只留在顶部红框，避免和阶段名抢位置
    for start, end in artifacts:
        for x in (x_of(t[start]), x_of(t[end])):
            for offset in range(band_top + 34, strip_top, 8):
                draw.line([x, offset, x, min(offset + 4, strip_top)], fill="#e0a0a0")
    draw.text((margin_l, strip_top - 24), "端侧 9 类模型当时的输出（每 100 ms 一格）",
              font=f15, fill="#444444")
    draw.rectangle([margin_l, strip_top, margin_l + plot_w, strip_top + 26],
                   fill="#fafafa", outline="#cccccc")
    ctx_colors = {"other": "#b8b8b8", "wind": "#4caf6d", "music": "#e08a5a",
                  "unknown": "#ffffff"}
    for i in range(len(t)):
        draw.rectangle([x_of(t[i]), strip_top, x_of(t[i]) + 2, strip_top + 26],
                       fill=ctx_colors.get(context[i], "#888888"))
    for start, end in artifacts:
        draw.rectangle([x_of(t[start]), strip_top, x_of(t[end]), strip_top + 26],
                       outline="#c0392b", width=2)
    for x in range(0, int(t1) + 1, 10):
        draw.text((x_of(x), strip_top + 78), f"{x}s", font=f13, fill="#999999", anchor="mt")
        draw.line([x_of(x), strip_top + 56, x_of(x), strip_top + 66], fill="#cccccc")

    legend_x = margin_l + 4
    for name, color, outline in (("other 其他", "#b8b8b8", "#999999"),
                                 ("wind 风", "#4caf6d", "#999999"),
                                 ("unknown 窗未满", "#ffffff", "#999999"),
                                 ("music 音乐", "#e08a5a", "#999999"),
                                 ("红框 = 撞击/削波（已剔除）", "#ffffff", "#c0392b")):
        draw.rectangle([legend_x, strip_top + 38, legend_x + 16, strip_top + 52],
                       fill=color, outline=outline)
        draw.text((legend_x + 22, strip_top + 45), name, font=f13, fill="#333333", anchor="lm")
        legend_x += 190

    draw.text((margin_l, height - 40),
              "CoreS3 ES7210 麦克风 · 24 段环境谱 10 Hz · 采集 tools/live_band_capture.py · 分析 tools/analyze_band_capture.py",
              font=f13, fill="#888888")
    # 阶段说明从数据里拼，别硬编码——同一张图会被不同协议的采集复用。
    segments = []
    segment_start = 0
    for index in range(1, len(phase) + 1):
        if index == len(phase) or phase[index] != phase[segment_start]:
            label = PHASE_CN.get(phase[segment_start], phase[segment_start])
            segments.append(f"{label} {t[index - 1] - t[segment_start]:.0f}s")
            segment_start = index
    draw.text((margin_l, height - 22), "阶段：" + " → ".join(segments), font=f13, fill="#888888")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"\n图 -> {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="分析 24 段谱采集 CSV")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--png", type=Path, default=None)
    parser.add_argument("--guard", type=float, default=2.0, help="每段开头剔除多少秒")
    parser.add_argument("--include-invalid", action="store_true",
                        help="把撞击/窗未满帧也算进统计（默认剔除）")
    args = parser.parse_args()

    rows, t, phase, bands, valid = load(args.csv)
    desc = descriptors(bands)
    guard = guard_mask(t, phase, args.guard)
    report(rows, t, phase, bands, desc, guard, valid, args.include_invalid)
    if args.png:
        render(rows, t, phase, desc, args.png, valid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
