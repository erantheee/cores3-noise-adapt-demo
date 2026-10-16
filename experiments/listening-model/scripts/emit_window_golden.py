"""生成 test/context_window_golden.h：训练口径下的 92 维窗向量。

为什么要有这个文件：`main/context_window.hpp` 是 `features.py::window_vector()` 的手工移植，
而**移植错一个索引不会报错**——设备只会安静地给出不同的类别。这里让训练那一侧的权威实现
（features.py）跑一遍固定配方，把结果落成 C++ 数组；`test/context_window_test.cpp` 再用
固件那份实现跑同一个配方，逐维对比。两份实现不一致时，CI 里的主机单测会红。

配方（C++ 与 Python 两边逐字相同，全部用二进制可精确表示的数（0.75/0.25/0.375/0.015625/0.03125/0.125），避免跨语言浮点差异）：

    帧 i = 0..19（i=0 最旧）
      band_dbfs[b]     = -60 - 2b - 0.5i        b = 0..23
      broadband_dbfs   = -50 + 0.5i
      voice_product    = (i % 3 == 0) ? 0.75 : 0.25    ← 未过时域闸门的逐帧乘积
      spectral_flatness=  0.5 - 0.015625i
      vocal_peakiness  =  0.25 + 0.03125i
      vocal_ratio      =  0.375 + 0.03125i
      excited_bands    =  i % 7
      t_s              =  100 + 0.125i          （秒）

用法：
    .venv/bin/python experiments/listening-model/scripts/emit_window_golden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from features import BAND_COUNT, FEATURE_COUNT, WINDOW_FRAMES, window_vector  # noqa: E402


def recipe() -> dict:
    n = WINDOW_FRAMES
    i = np.arange(n, dtype=np.float64)
    bands = np.zeros((n, BAND_COUNT), dtype=np.float64)
    for b in range(BAND_COUNT):
        bands[:, b] = -60.0 - 2.0 * b - 0.5 * i
    return {
        "t_s": 100.0 + 0.125 * i,
        "band_dbfs": bands,
        "broadband_dbfs": -50.0 + 0.5 * i,
        "voice_likelihood": np.where(i % 3 == 0, 0.75, 0.25),
        "spectral_flatness": 0.5 - 0.015625 * i,
        "vocal_peakiness": 0.25 + 0.03125 * i,
        "vocal_ratio": 0.375 + 0.03125 * i,
        "excited_vocal_bands": (i % 7).astype(np.float64),
    }


def literal(v: float) -> str:
    s = f"{float(v):.9g}"
    if not any(c in s for c in ".eE"):
        s += ".0"
    return s + "f"


def main() -> None:
    vec = window_vector(recipe(), 0, WINDOW_FRAMES, motion=None, when=None)
    assert vec.shape == (FEATURE_COUNT,), vec.shape
    out = SCRIPTS.parent.parent.parent / "test" / "context_window_golden.h"
    lines = [
        "// 由 experiments/listening-model/scripts/emit_window_golden.py 自动生成，请勿手改。",
        "// 来源：features.py::window_vector()（训练口径）在固定配方上的输出。",
        "// 配方写在生成脚本头部，与 test/context_window_test.cpp 里的 rebuild 逐字一致。",
        "#pragma once",
        "",
        "namespace context_golden {",
        f"constexpr int kFeatureCount = {FEATURE_COUNT};",
        "constexpr float kExpected[kFeatureCount] = {",
    ]
    for start in range(0, FEATURE_COUNT, 6):
        row = ", ".join(literal(v) for v in vec[start : start + 6])
        lines.append(f"    {row},")
    lines += ["};", "}  // namespace context_golden", ""]
    out.write_text("\n".join(lines))
    print(f"{FEATURE_COUNT} 维 -> {out}")
    print("level_mean/level_std/min/max:", np.round(vec[72:76], 6))
    print("vl_mean/std/active/transitions:", np.round(vec[76:80], 6))
    print("time[87:92]:", np.round(vec[87:92], 6))
    print("argmax 需要模型，本脚本只固化向量。")


if __name__ == "__main__":
    main()
