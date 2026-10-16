#!/usr/bin/env python3
"""Probe a lv_font_conv-generated LVGL font .c file.

Renders one glyph twice: once assuming PLAIN bpp packing, once assuming LVGL's
RLE (bitmap_format == LV_FONT_FMT_TXT_COMPRESSED) stream. Whichever produces a
recognizable character tells us what the data really is.
"""
import re
import sys

OPA4 = [int(round(i * 255 / 15)) for i in range(16)]


def parse(path):
    src = open(path, encoding="utf-8").read()

    m = re.search(r"glyph_bitmap\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    bitmap = bytes(int(x, 0) for x in re.findall(r"0[xX][0-9a-fA-F]+|\d+", body))

    m = re.search(r"glyph_dsc\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    gdsc = []
    for entry in re.findall(r"\{([^{}]*)\}", m.group(1)):
        f = dict(re.findall(r"\.(\w+)\s*=\s*(-?\d+)", entry))
        if not f:
            continue
        gdsc.append({k: int(v) for k, v in f.items()})

    ulists = {}
    for um in re.finditer(r"unicode_list_(\d+)\[\]\s*=\s*\{(.*?)\n\};", src, re.S):
        ulists["unicode_list_" + um.group(1)] = [
            int(x, 0) for x in re.findall(r"0[xX][0-9a-fA-F]+|\d+", um.group(2))]

    cmaps = []
    m = re.search(r"lv_font_fmt_txt_cmap_t cmaps\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    for entry in re.findall(r"\{\s*(.*?)\}\s*,?\s*(?=\{|\Z)", m.group(1), re.S):
        def g(k, d=None):
            mm = re.search(r"\.%s\s*=\s*(\d+)" % k, entry)
            return int(mm.group(1)) if mm else d
        tm = re.search(r"\.type\s*=\s*(\w+)", entry)
        um = re.search(r"\.unicode_list\s*=\s*(\w+)", entry)
        cmaps.append({
            "range_start": g("range_start"),
            "range_length": g("range_length", 0),
            "glyph_id_start": g("glyph_id_start", 0),
            "list_length": g("list_length", 0),
            "tname": tm.group(1) if tm else "",
            "ulist": ulists.get(um.group(1), []) if um else [],
        })

    bm = re.search(r"\.bitmap_format\s*=\s*(\d+)", src)
    bpp = re.search(r"\.bpp\s*=\s*(\d+)", src)
    nm = re.search(r"lv-font-name\s+(\S+)", src) or re.search(r"const lv_font_t (\w+)", src)
    return {
        "name": nm.group(1) if nm else path,
        "bitmap": bitmap,
        "gdsc": gdsc,
        "cmaps": cmaps,
        "bitmap_format": int(bm.group(1)) if bm else None,
        "bpp": int(bpp.group(1)) if bpp else None,
    }


def glyph_id(font, cp):
    for c in font["cmaps"]:
        rs, rl, gs = c["range_start"], c["range_length"], c["glyph_id_start"]
        if rs is None:
            continue
        if c["tname"].endswith("SPARSE_TINY"):
            if rs <= cp < rs + rl:
                rcp = cp - rs
                if rcp in c["ulist"]:
                    return gs + c["ulist"].index(rcp)
        else:
            if rs <= cp < rs + rl:
                return gs + (cp - rs)
    return None


def glyph_bytes(font, gid):
    start = font["gdsc"][gid]["bitmap_index"]
    end = font["gdsc"][gid + 1]["bitmap_index"] if gid + 1 < len(font["gdsc"]) else None
    if end is None or end < start:
        end = start + 512
    return font["bitmap"][start:end][:end - start]


def decode_plain(data, w, h, bpp):
    """LVGL PLANE path: rows packed continuously, no stride."""
    out, bit = [], 0
    for _ in range(h):
        row = []
        for _ in range(w):
            byte = data[bit >> 3]
            shift = bit & 7
            if shift + bpp > 8:
                v = ((byte << 8) | data[(bit >> 3) + 1]) >> (16 - shift - bpp)
            else:
                v = byte >> (8 - shift - bpp)
            row.append((v & ((1 << bpp) - 1)))
            bit += bpp
        out.append(row)
    return out


def decode_rle(data, w, h, bpp, prefilter):
    """Port of lv_font_fmt_txt.c decompress()."""
    def get_bits(bit_pos, ln):
        byte_pos, off = bit_pos >> 3, bit_pos & 7
        if off + ln >= 8:
            v = (data[byte_pos] << 8) + data[byte_pos + 1]
            return (v >> (16 - off - ln)) & ((1 << ln) - 1)
        return (data[byte_pos] >> (8 - off - ln)) & ((1 << ln) - 1)

    state = {"rdp": 0, "prev": 0, "count": 0, "mode": "SINGLE"}

    def nxt():
        if state["mode"] == "SINGLE":
            ret = get_bits(state["rdp"], bpp)
            if state["rdp"] != 0 and state["prev"] == ret:
                state["count"] = 0
                state["mode"] = "REPEATED"
            state["prev"] = ret
            state["rdp"] += bpp
            return ret
        if state["mode"] == "REPEATED":
            v = get_bits(state["rdp"], 1)
            state["count"] += 1
            state["rdp"] += 1
            if v == 1:
                ret = state["prev"]
                if state["count"] == 11:
                    state["count"] = get_bits(state["rdp"], 6)
                    state["rdp"] += 6
                    if state["count"] != 0:
                        state["mode"] = "COUNTER"
                    else:
                        ret = get_bits(state["rdp"], bpp)
                        state["prev"] = ret
                        state["rdp"] += bpp
                        state["mode"] = "SINGLE"
                return ret
            ret = get_bits(state["rdp"], bpp)
            state["prev"] = ret
            state["rdp"] += bpp
            state["mode"] = "SINGLE"
            return ret
        # COUNTER
        ret = state["prev"]
        state["count"] -= 1
        if state["count"] == 0:
            ret = get_bits(state["rdp"], bpp)
            state["prev"] = ret
            state["rdp"] += bpp
            state["mode"] = "SINGLE"
        return ret

    rows, prev = [], None
    for _ in range(h):
        line = [nxt() for _ in range(w)]
        if prefilter and prev is not None:
            line = [a ^ b for a, b in zip(line, prev)]
        prev = line
        rows.append(line)
    return rows


def art(rows, bpp=4):
    shade = " .:-=+*#%@"
    return "\n".join(
        "".join(shade[min(9, r * 10 // (1 << bpp))] for r in row) for row in rows
    )


def main():
    path, chars = sys.argv[1], sys.argv[2]
    font = parse(path)
    print("font=%s bitmap_format=%s bpp=%s glyphs=%d bitmap_bytes=%d"
          % (font["name"], font["bitmap_format"], font["bpp"],
             len(font["gdsc"]) - 1, len(font["bitmap"])))
    for ch in chars:
        gid = glyph_id(font, ord(ch))
        d = font["gdsc"][gid]
        data = glyph_bytes(font, gid)
        w, h = d["box_w"], d["box_h"]
        print("\n--- %r U+%04X gid=%d box=%dx%d bytes=%d ---"
              % (ch, ord(ch), gid, w, h, len(data)))
        if not w or not h:
            continue
        need = (w * h * font["bpp"] + 7) // 8
        print("[PLAIN %dbpp] needs %d bytes, have %d" % (font["bpp"], need, len(data)))
        try:
            print(art(decode_plain(data, w, h, font["bpp"])))
        except IndexError:
            print("  <ran out of data: stored bitmap is smaller than plain>")
        print("[RLE prefilter=%s]" % (font["bitmap_format"] == 1))
        try:
            print(art(decode_rle(data, w, h, font["bpp"], font["bitmap_format"] == 1)))
        except IndexError:
            print("  <ran out of data>")


if __name__ == "__main__":
    main()
