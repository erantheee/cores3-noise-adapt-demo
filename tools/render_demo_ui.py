#!/usr/bin/env python3
"""Render main/demo_ui.cpp's main page straight from the real font bitmaps.

This is NOT a screenshot. It re-implements the subset of LVGL that demo_ui.cpp uses --
card rectangles at fixed coordinates, label placement, clipping to the label width -- and
blits the actual glyph bitmaps decoded from the same .c fonts the firmware compiles
(`main/lv_font_ui_16.c` and LVGL's built-in montserrat_14/20).

Why it exists: this round added a card to a 320x240 screen that was already full, and the
one thing that can go wrong there fails silently -- LVGL clips text that does not fit, with
no error anywhere, and the device screen sits inside a case. So the widths are checked here,
with the longest string each card can ever be asked to draw.

No third-party dependency on purpose: Pillow is not installed in any interpreter on this
machine, and a preview tool is not worth a dependency. The PNG writer below is 15 lines of
zlib + struct from the standard library.

Caveat, same as tools/render_ear_ui.py: widget radii, borders and the bar track are LVGL
theme approximations, not a pixel-exact copy of the device.

    python3 tools/render_demo_ui.py                    # measured device state
    python3 tools/render_demo_ui.py --worst-case       # longest string per card
    python3 tools/render_demo_ui.py --out outputs/demo-ui-fusion-context.png
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font_probe import decode_plain, glyph_id, parse  # noqa: E402

SCALE = 2
W, H = 320, 240

C_SCREEN = (0x0B, 0x0D, 0x11)
C_CARD = (0x17, 0x1A, 0x20)
C_CARD_EDGE = (0x2A, 0x30, 0x3A)
C_TITLE = (0x8E, 0x99, 0xA8)
C_VALUE = (0xFF, 0xFF, 0xFF)
C_GREEN = (0x5F, 0xF2, 0xA7)
C_DIM = (0x69, 0x73, 0x84)
C_MUTED = (0x9B, 0xA5, 0xB4)
C_TRACK = (0x23, 0x28, 0x33)

FONT_DIR = "managed_components/lvgl__lvgl/src/font"


class Canvas:
    def __init__(self, w: int, h: int, background):
        self.w, self.h = w, h
        self.buf = bytearray(background * (w * h))

    def pixel(self, x: int, y: int, color):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) * 3
        self.buf[i:i + 3] = bytes(color)

    def blend(self, x: int, y: int, color, alpha: float):
        if not (0 <= x < self.w and 0 <= y < self.h) or alpha <= 0:
            return
        i = (y * self.w + x) * 3
        for k in range(3):
            self.buf[i + k] = int(self.buf[i + k] + (color[k] - self.buf[i + k]) * alpha)

    def rounded_rect(self, x0, y0, x1, y1, radius, fill, outline=None):
        """x1/y1 exclusive, in canvas pixels."""
        x0, y0, x1, y1, radius = int(x0), int(y0), int(x1), int(y1), int(radius)
        for y in range(y0, y1):
            for x in range(x0, x1):
                dx = max(x0 + radius - x, x - (x1 - 1 - radius), 0)
                dy = max(y0 + radius - y, y - (y1 - 1 - radius), 0)
                if dx * dx + dy * dy > radius * radius:
                    continue
                if outline is not None and (x in (x0, x1 - 1) or y in (y0, y1 - 1)):
                    self.pixel(x, y, outline)
                else:
                    self.pixel(x, y, fill)

    def png(self, path: str):
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)  # filter type 0
            raw += self.buf[y * stride:(y + 1) * stride]

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        blob = b"\x89PNG\r\n\x1a\n"
        blob += chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
        blob += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        blob += chunk(b"IEND", b"")
        with open(path, "wb") as fh:
            fh.write(blob)


class Font:
    """Glyph metrics + bitmaps, placed with LVGL's lv_draw_label formula."""

    def __init__(self, path: str):
        self.f = parse(path)
        src = open(path, encoding="utf-8").read()
        self.line_height = int(re.search(r"\.line_height\s*=\s*(\d+)", src).group(1))
        self.base_line = int(re.search(r"\.base_line\s*=\s*(-?\d+)", src).group(1))
        self._cache = {}

    def glyph(self, ch: str):
        cp = ord(ch)
        if cp in self._cache:
            return self._cache[cp]
        gid = glyph_id(self.f, cp)
        if gid is None:
            self._cache[cp] = None
            return None
        d = self.f["gdsc"][gid]
        w, h = d["box_w"], d["box_h"]
        px = None
        if w and h:
            start = d["bitmap_index"]
            end = (self.f["gdsc"][gid + 1]["bitmap_index"]
                   if gid + 1 < len(self.f["gdsc"]) else start + 4096)
            px = decode_plain(self.f["bitmap"][start:end], w, h, self.f["bpp"])
        g = (w, h, d["ofs_x"], d["ofs_y"], (d["adv_w"] + 8) >> 4, px)
        self._cache[cp] = g
        return g

    def text_width(self, text: str) -> int:
        total = 0
        for ch in text:
            g = self.glyph(ch)
            total += g[4] if g else self.line_height // 2
        return total

    def missing(self, text: str) -> set:
        return {ch for ch in text if self.glyph(ch) is None}


def draw_text(cv: Canvas, font: Font, x: int, y: int, text: str, color, max_width=None) -> int:
    """Returns the width actually drawn, so the caller can tell clipped text from fitting text."""
    pen_x = x
    for ch in text:
        g = font.glyph(ch)
        if g is None:
            continue
        bw, bh, ofs_x, ofs_y, adv, px = g
        if max_width is not None and pen_x - x + adv > max_width:
            break
        if px:
            top = y + (font.line_height - font.base_line) - bh - ofs_y
            left = pen_x + ofs_x
            for ry, row in enumerate(px):
                for rx, v in enumerate(row):
                    if v:
                        cv.blend(left + rx, top + ry, color, v / 15.0)
        pen_x += adv
    return pen_x - x


def card(cv: Canvas, fonts, x, y, w, h, title, value, value_font, value_color=C_VALUE):
    """Mirrors make_card() in main/demo_ui.cpp: pad 6, title at the content origin,
    value at (0, 25) inside the content area, label width w - 14 (both in screen px)."""
    cv.rounded_rect(x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE,
                    radius=10 * SCALE, fill=C_CARD, outline=C_CARD_EDGE)
    inner = w - 14
    draw_text(cv, fonts["ui16"], (x + 6) * SCALE, (y + 6) * SCALE, title, C_TITLE)
    needs = value_font.text_width(value)
    draw_text(cv, value_font, (x + 6) * SCALE, (y + 31) * SCALE, value, value_color,
              max_width=inner * SCALE)
    return {"title": title, "value": value, "box": inner, "needs": needs, "fits": needs <= inner}


def render(state: dict, out_path: str):
    cv = Canvas(W * SCALE, H * SCALE, C_SCREEN)
    fonts = {
        "ui16": Font("main/lv_font_ui_16.c"),
        "m14": Font(f"{FONT_DIR}/lv_font_montserrat_14.c"),
        "m20": Font(f"{FONT_DIR}/lv_font_montserrat_20.c"),
    }

    draw_text(cv, fonts["ui16"], 10 * SCALE, 7 * SCALE, "端侧算法验证机", C_VALUE)
    draw_text(cv, fonts["ui16"], 270 * SCALE, 7 * SCALE, "自动", C_GREEN)

    checks = [
        card(cv, fonts, 10, 31, 80, 60, "环境", state["env"], fonts["ui16"]),
        card(cv, fonts, 96, 31, 112, 60, "当前动作", state["action"], fonts["ui16"]),
        card(cv, fonts, 214, 31, 96, 60, state["context_title"], state["context"],
             fonts["m14"], state["context_color"]),
        card(cv, fonts, 10, 97, 96, 60, "人声状态", state["voice"], fonts["m20"]),
        card(cv, fonts, 112, 97, 96, 60, "调音", state["tune"], fonts["m20"]),
        card(cv, fonts, 214, 97, 96, 60, "人声增益", state["gain"], fonts["m20"]),
        card(cv, fonts, 10, 163, 96, 52, "控制链路", state["link"], fonts["ui16"]),
    ]

    draw_text(cv, fonts["ui16"], 112 * SCALE, 158 * SCALE, state["detail"], C_GREEN)
    draw_text(cv, fonts["ui16"], 112 * SCALE, 179 * SCALE, state["perf"], C_MUTED)
    cv.rounded_rect(112 * SCALE, 200 * SCALE, (112 + 198) * SCALE, 208 * SCALE,
                    radius=4 * SCALE, fill=C_TRACK)
    bar = int(198 * state["confidence"])
    if bar > 0:
        cv.rounded_rect(112 * SCALE, 200 * SCALE, (112 + bar) * SCALE, 208 * SCALE,
                        radius=4 * SCALE, fill=C_GREEN)

    footer = "> 2/2"
    fw = fonts["ui16"].text_width(footer)
    draw_text(cv, fonts["ui16"], ((W - fw) // 2) * SCALE,
              (H - 4 - fonts["ui16"].line_height) * SCALE, footer, C_DIM)

    cv.png(out_path)
    return fonts, checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/demo-ui-fusion-context.png")
    ap.add_argument("--worst-case", action="store_true",
                    help="每格都画它能拿到的最长字符串（宽度校验用）")
    args = ap.parse_args()

    if args.worst_case:
        state = {
            "env": "人声嘈杂",            # environment_text 最长项
            "action": "保持当前设置",      # action_text 最长项
            "context_title": "感知 0.87",
            "context": "near_voice",      # kClassNames 最长项
            "context_color": C_GREEN,
            "voice": "LIKELY",
            "tune": "+8.0 dB",
            "gain": "+8.0 dB",
            "link": "未连接",
            "detail": "麦克风正常",
            "perf": "处理 12.3ms  内存 9999k",
            "confidence": 1.0,
        }
        out = args.out if args.out != "outputs/demo-ui-fusion-context.png" \
            else "outputs/demo-ui-fusion-context-worstcase.png"
    else:
        # 2026-10-16 11:2x 真机实测（安静房间）的那一帧。
        state = {
            "env": "人声嘈杂",
            "action": "人声提升",
            "context_title": "感知 0.45",
            "context": "other",
            "context_color": C_VALUE,
            "voice": "LIKELY",
            "tune": "+3.7 dB",
            "gain": "+3.7 dB",
            "link": "未连接",
            "detail": "麦克风正常",
            "perf": "处理 8.1ms  内存 214k",
            "confidence": 0.45,
        }
        out = args.out

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fonts, checks = render(state, out)

    print(f"-> {out}")
    print("宽度校验（值标签可用宽度 = 卡片宽 - 14）：")
    ok = True
    for c in checks:
        if not c["fits"]:
            ok = False
        print(f"  {'OK  ' if c['fits'] else 'CLIP'} {c['title']:<9} '{c['value']}' "
              f"需要 {c['needs']:>3} / 可用 {c['box']:>3} px")
    # 每个字库只检查它真正要画的那串字。把中文丢给 montserrat 检查是错的：它本来就没有 CJK，
    # 而感知卡的值行**故意**用 montserrat_14 —— 类别名是 ASCII，永远不会撞上缺字形。
    checks_by_font = {
        "ui16": ("端侧算法验证机自动环境当前动作感知人声状态调音增益控制链路麦克风正常处理内存"
                 "未连接人声嘈杂人声提升保持当前设置感知> 2/2"),
        "m14": "near_voice babble music horn other quiet steady street wind --",
        "m20": "LIKELY NONE SPEECH +8.0 dB",
    }
    missing = set()
    for name, text in checks_by_font.items():
        gone = fonts[name].missing(text)
        if gone:
            print(f"  {name} 缺字形：", "".join(sorted(gone)))
            missing |= gone
        ok = ok and not gone
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
