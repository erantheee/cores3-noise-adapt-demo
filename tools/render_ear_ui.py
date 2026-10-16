#!/usr/bin/env python3
"""Render the ear-context UI pages straight from the real font bitmaps.

This is NOT a screenshot: it re-implements the small subset of LVGL that ui.cpp uses
(positions, colors, per-glyph placement) and blits the actual glyph bitmaps decoded from
main/ear_font16.c and main/ear_font24.c. If a glyph failed to decode, the page would come
out with blank text -- which is exactly the defect this render is used to check.

Glyph placement follows lv_draw_label.c:
    glyph_top = line_top + (line_height - base_line) - box_h - ofs_y
    glyph_left = pen_x + ofs_x ;  pen_x += (adv_w + 8) >> 4
Widget radii / bar-track colors come from the LVGL default theme and are approximations.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font_probe import parse, glyph_id, decode_plain  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

SCALE = 2
W, H = 320, 240
SKIP_TEXT = False   # set to True to draw the page as the broken (compressed-font) build did
C_ROOT = (0x09, 0x15, 0x20)
C_PANEL = (0x15, 0x25, 0x36)
C_TEXT = (0xDC, 0xE8, 0xF2)
C_TEAL = (0x64, 0xDD, 0xD0)
C_MUTED = (0x90, 0xA7, 0xBA)
C_WARN = (0xFF, 0xB8, 0x6B)
C_BTN = (0x25, 0x40, 0x52)
C_TRACK = (0x2A, 0x3A, 0x4A)


class Font:
    def __init__(self, path):
        self.f = parse(path)
        self.f["_cache"] = {}
        # line_height / base_line as declared in the generated descriptor
        src = open(path, encoding="utf-8").read()
        import re
        self.line_height = int(re.search(r"\.line_height\s*=\s*(\d+)", src).group(1))
        self.base_line = int(re.search(r"\.base_line\s*=\s*(-?\d+)", src).group(1))

    def glyph(self, ch):
        cp = ord(ch)
        if cp in self.f["_cache"]:
            return self.f["_cache"][cp]
        gid = glyph_id(self.f, cp)
        if gid is None:
            self.f["_cache"][cp] = None
            return None
        d = self.f["gdsc"][gid]
        w, h = d["box_w"], d["box_h"]
        px = None
        if w and h:
            start = d["bitmap_index"]
            end = (self.f["gdsc"][gid + 1]["bitmap_index"]
                   if gid + 1 < len(self.f["gdsc"]) else start + 512)
            px = decode_plain(self.f["bitmap"][start:end], w, h, self.f["bpp"])
        g = (w, h, d["ofs_x"], d["ofs_y"], (d["adv_w"] + 8) >> 4, px)
        self.f["_cache"][cp] = g
        return g

    def text_width(self, s):
        return sum((self.glyph(c) or (0, 0, 0, 0, self.line_height // 2, None))[4] for c in s)


def draw_text(dr, font, x, y, text, color, line_space=0, max_width=None):
    """Draws with the LVGL fmt_txt placement formula.

    With SKIP_TEXT set, reproduces the defect: glyph lookup still succeeds (so no
    placeholder box appears) but the bitmap comes back NULL and the draw unit skips the
    glyph, leaving every text widget empty.
    """
    if SKIP_TEXT:
        return
    pen_y = y
    for line in text.split("\n"):
        pen_x = x
        for ch in line:
            g = font.glyph(ch)
            if g is None:
                pen_x += font.line_height // 2
                continue
            bw, bh, ofs_x, ofs_y, adv, px = g
            if px:
                top = pen_y + (font.line_height - font.base_line) - bh - ofs_y
                left = pen_x + ofs_x
                for ry, row in enumerate(px):
                    for rx, v in enumerate(row):
                        if v:
                            a = v / 15.0
                            bx, by = (left + rx) * SCALE, (top + ry) * SCALE
                            base = dr._image.getpixel((min(bx, W * SCALE - 1), min(by, H * SCALE - 1)))
                            dr.rectangle([bx, by, bx + SCALE - 1, by + SCALE - 1],
                                         fill=tuple(int(b + (c - b) * a) for b, c in zip(base, color)))
            pen_x += adv
            if max_width and pen_x - x > max_width:
                break
        pen_y += font.line_height + line_space


def panel(dr, x, y, w, h, r=12):
    dr.rounded_rectangle([x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE],
                         radius=r * SCALE, fill=C_PANEL)


def page(dr, f16, f24, *, detail, scene, subtitle, level, level_pct, motion, motion_ok):
    dr.rectangle([0, 0, W * SCALE, H * SCALE], fill=C_ROOT)
    draw_text(dr, f16, 12, 8, "耳戴 · 环境感知", C_TEAL)
    draw_text(dr, f16, 257, 8, "实验版", C_MUTED)

    if not detail:
        panel(dr, 10, 34, 300, 86)
        draw_text(dr, f16, 22, 40, "当前环境 · 模型判断", C_MUTED)
        draw_text(dr, f24, 22, 62, scene, C_TEXT)
        draw_text(dr, f16, 22, 95, subtitle, C_MUTED)

        panel(dr, 10, 128, 145, 70)
        draw_text(dr, f16, 20, 134, "声音电平", C_MUTED)
        draw_text(dr, f16, 20, 155, level, C_TEXT)
        dr.rectangle([30, 181 * SCALE, (10 + 125) * SCALE, 186 * SCALE], fill=C_TRACK)
        dr.rectangle([30, 181 * SCALE, (10 + 125 * level_pct / 100) * SCALE, 186 * SCALE], fill=C_TEAL)

        panel(dr, 163, 128, 147, 70)
        draw_text(dr, f16, 173, 134, "头部传感器", C_MUTED)
        draw_text(dr, f16, 173, 155, motion, C_TEAL if motion_ok else C_WARN)
        draw_text(dr, f16, 173, 177, "活动识别待训练", C_MUTED)
    else:
        draw_text(dr, f16, 16, 37, "声音候选：%s" % scene, C_TEXT)
        draw_text(dr, f16, 16, 37 + 24, "头部转动：1.3 度/秒", C_TEXT)
        draw_text(dr, f16, 16, 37 + 48, "模型计算：0.09 毫秒", C_TEXT)
        draw_text(dr, f16, 16, 37 + 72, "输出丢弃：0 条", C_TEXT)
        draw_text(dr, f16, 16, 37 + 96, "时钟未同步 · 活动模型待训练", C_TEXT)
        draw_text(dr, f16, 16, 37 + 120, "USB 数据输出 · 未确认保存", C_TEXT)

    draw_text(dr, f16, 12, 212, "仅感知 · 耳机未连接", C_MUTED)
    dr.rounded_rectangle([246 * SCALE, 206 * SCALE, 310 * SCALE, 236 * SCALE],
                         radius=4 * SCALE, fill=C_BTN)
    nav = "返回" if detail else "详情"
    nx = 246 + (64 - f16.text_width(nav)) // 2
    draw_text(dr, f16, nx, 206 + (30 - f16.line_height) // 2, nav, C_TEXT)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    f16 = Font(os.path.join(root, "experiments/ear-context/main/ear_font16.c"))
    f24 = Font(os.path.join(root, "experiments/ear-context/main/ear_font24.c"))
    print("ear_font16 line_height=%d base_line=%d  ear_font24 line_height=%d base_line=%d"
          % (f16.line_height, f16.base_line, f24.line_height, f24.base_line))

    for label, font, sample in (("ear_font16", f16, "耳戴 · 环境感知 模型分数 82%"),
                                ("ear_font24", f24, "嘈杂人声")):
        missing = [c for c in sample if font.glyph(c) is None]
        print("%s renders %r, undecodable glyphs: %s" % (label, sample, missing or "none"))

    pages = [
        dict(detail=False, scene="嘈杂人声", subtitle="模型分数 82% · 真实效果待验证",
             level="-38.4 dBFS", level_pct=62, motion="六轴采集中", motion_ok=True),
        dict(detail=True, scene="嘈杂人声", subtitle="", level="", level_pct=0,
             motion="", motion_ok=True),
    ]
    global SKIP_TEXT
    gap, rows = 20, 2
    canvas = Image.new("RGB", (W * SCALE * len(pages) + gap * (len(pages) - 1),
                              (H * SCALE + gap) * rows - gap), (0, 0, 0))
    for row, (skip, tag) in enumerate(((True, "BEFORE"), (False, "AFTER"))):
        SKIP_TEXT = skip
        for i, p in enumerate(pages):
            img = Image.new("RGB", (W * SCALE, H * SCALE), C_ROOT)
            page(ImageDraw.Draw(img), f16, f24, **p)
            canvas.paste(img, (i * (W * SCALE + gap), row * (H * SCALE + gap)))
        print("row %d (%s): text drawn = %s" % (row, tag, "no" if skip else "yes"))
    SKIP_TEXT = False
    out = os.path.join(root, "outputs/ear-ui-fontfix-preview.png")
    canvas.save(out)
    print("wrote", out, canvas.size)


if __name__ == "__main__":
    main()
