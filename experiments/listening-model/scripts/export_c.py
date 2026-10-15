"""把训练好的模型导成固件可直接 include 的 C++ 头文件。

两个关键动作：
  1. **归一化折进第一层**：x_norm = (x - mu)/sigma 等价于 W1' = W1/sigma、b1' = b1 - W1'·mu。
     设备端因此只需要一次矩阵乘，不必在固件里再实现一套归一化。
  2. **导出后回验**：用 numpy 重新实现一遍折叠后的网络，逐样本与 PyTorch 的输出对比。
     导出物没有经过这道回验，就不算可用产物——C 数组里错一个小数点，设备上不会报错，
     只会安静地给出错误决策。

用法：
    python scripts/export_c.py --model artifacts/model_v0.1.pt --header <路径>/listening_model.h
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch


def fuse_normalization(state, mu, sigma):
    W1 = state["net.0.weight"].numpy()
    b1 = state["net.0.bias"].numpy()
    W1f = W1 / sigma[None, :]
    b1f = b1 - W1f @ mu
    return W1f, b1f


def np_forward(x, W1, b1, W2, b2, W3, b3):
    h1 = np.maximum(x @ W1.T + b1, 0.0)
    h2 = np.maximum(h1 @ W2.T + b2, 0.0)
    return h2 @ W3.T + b3


def quantize_int8(w):
    scale = float(np.abs(w).max()) / 127.0 or 1.0
    q = np.clip(np.round(w / scale), -127, 127).astype(np.int8)
    return q, scale


def emit_array(fh, name, values, per_line=8):
    flat = values.reshape(-1)
    fh.write(f"constexpr float {name}[{flat.size}] = {{\n    ")
    for i, v in enumerate(flat):
        fh.write(f"{_literal(v)},")
        if (i + 1) % per_line == 0:
            fh.write("\n    ")
    fh.write("\n};\n\n")


def _literal(v) -> str:
    """C++ 浮点字面量：整数型写法（120、0、5）必须补小数点，否则 120f 不是合法字面量。"""
    s = f"{float(v):.9g}"
    if not any(c in s for c in ".eE"):
        s += ".0"
    return s + "f"


def emit_test_vectors(path: Path, dataset: Path, per_class: int, feature_count: int,
                      weights):
    """把验证集样本导成设备端自检向量。

    期望值是**主机 numpy 前向的 argmax**，不是数据集标签：这一步验证的是"设备端 C 代码与
    主机数值路径一致"，与模型准确率是两回事（约 95% 的验证集本身就有约 5% 会被模型自己
    分错，拿标签当期望值会把模型误差误报成导出错误）。
    """
    d = np.load(dataset, allow_pickle=True)
    classes = [str(c) for c in d["classes"]]
    X = d["X_val"] if len(d["X_val"]) else d["X_train"]
    y = d["y_val"] if len(d["y_val"]) else d["y_train"]
    picks = []
    for ci in range(len(classes)):
        idx = np.where(y == ci)[0][:per_class]
        picks.extend(int(i) for i in idx)
    if not picks:
        raise SystemExit("验证集为空，无法导出自检向量")
    W1, b1, W2, b2, W3, b3 = weights
    expected = []
    for i in picks:
        logits = np_forward(X[i][None, :], W1, b1, W2, b2, W3, b3)
        expected.append(int(logits.argmax(axis=1)[0]))

    with path.open("w") as fh:
        fh.write("// 由 scripts/export_c.py 自动生成，请勿手改。\n")
        fh.write(f"// 来源: {dataset.name} 的验证集样本（原始特征，未归一化）\n")
        fh.write("// 期望值 = 主机 numpy 前向的 argmax（验证数值路径一致，与准确率无关）\n")
        fh.write("#pragma once\n\n")
        fh.write("namespace test_vectors {\n")
        fh.write(f"constexpr int kCount = {len(picks)};\n")
        fh.write(f"constexpr int kFeatureCount = {feature_count};\n\n")
        fh.write("const float kData[kCount][kFeatureCount] = {\n")
        for i in picks:
            row = ", ".join(_literal(v) for v in X[i])
            fh.write(f"    {{{row}}},  // {classes[int(y[i])]}\n")
        fh.write("};\n\n")
        fh.write("const int kExpected[kCount] = {\n    ")
        fh.write(", ".join(str(e) for e in expected))
        fh.write("\n};\n}\n")
    return len(picks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--header", required=True)
    ap.add_argument("--card", default=None, help="模型卡片 JSON 路径（默认与 header 同目录）")
    ap.add_argument("--vectors", default=None, help="数据集 npz：导出设备端自检向量")
    ap.add_argument("--vectors-per-class", type=int, default=3)
    args = ap.parse_args()

    ckpt = torch.load(args.model, map_location="cpu", weights_only=False)
    state = ckpt["state_dict"]
    mu, sigma = ckpt["mu"], ckpt["sigma"]
    classes = ckpt["classes"]
    metrics = ckpt.get("metrics", {})
    feature_names = ckpt["feature_names"]

    W1, b1 = fuse_normalization(state, mu, sigma)
    W2 = state["net.3.weight"].numpy()
    b2 = state["net.3.bias"].numpy()
    W3 = state["net.6.weight"].numpy()
    b3 = state["net.6.bias"].numpy()

    # ---- 回验 1：折叠后网络的 float32 输出必须与 torch 一致 ----
    rng = np.random.default_rng(0)
    x = rng.normal(size=(512, W1.shape[1])).astype(np.float32) * 1.5
    logits_np = np_forward(x, W1, b1, W2, b2, W3, b3)
    with torch.no_grad():
        logits_pt = torch.nn.Sequential(
            torch.nn.Linear(W1.shape[1], W1.shape[0]),
            torch.nn.ReLU(),
            torch.nn.Linear(W2.shape[1], W2.shape[0]),
            torch.nn.ReLU(),
            torch.nn.Linear(W3.shape[1], W3.shape[0]),
        )
        logits_pt[0].weight.copy_(torch.tensor(W1))
        logits_pt[0].bias.copy_(torch.tensor(b1))
        logits_pt[2].weight.copy_(torch.tensor(W2))
        logits_pt[2].bias.copy_(torch.tensor(b2))
        logits_pt[4].weight.copy_(torch.tensor(W3))
        logits_pt[4].bias.copy_(torch.tensor(b3))
        ref = logits_pt(torch.tensor(x)).numpy()
    max_diff = float(np.abs(ref - logits_np).max())
    same = bool((ref.argmax(axis=1) == logits_np.argmax(axis=1)).all())

    # ---- 回验 2：逐张量对称 int8 量化后的精度损失（参考值，v0.1 不上 int8） ----
    W1q, s1 = quantize_int8(W1)
    W2q, s2 = quantize_int8(W2)
    W3q, s3 = quantize_int8(W3)
    logits_q = np_forward(x, W1q.astype(np.float32) * s1, b1,
                          W2q.astype(np.float32) * s2, b2,
                          W3q.astype(np.float32) * s3, b3)
    int8_agreement = float((logits_q.argmax(axis=1) == ref.argmax(axis=1)).mean())

    header = Path(args.header)
    header.parent.mkdir(parents=True, exist_ok=True)
    with header.open("w") as fh:
        fh.write("// 由 scripts/export_c.py 自动生成，请勿手改。\n")
        fh.write(f"// 模型: {metrics.get('model', 'ContextNet v0.1')}\n")
        fh.write("// 输入: 最近 2 秒窗特征（24 频段均值/标准差/斜率 + 电平 + 语音描述量 + 时间）\n")
        fh.write("// 归一化已折进第一层权重；端侧只需一次全连接前向。\n")
        fh.write("#pragma once\n\n#include <cstddef>\n\n")
        fh.write("namespace listening_model {\n\n")
        fh.write(f'constexpr const char* kVersion = "{metrics.get("version", "v0.1.0")}";\n')
        fh.write(f"constexpr std::size_t kFeatureCount = {W1.shape[1]};\n")
        fh.write(f"constexpr std::size_t kHidden1 = {W1.shape[0]};\n")
        fh.write(f"constexpr std::size_t kHidden2 = {W2.shape[0]};\n")
        fh.write(f"constexpr std::size_t kClassCount = {W3.shape[0]};\n\n")
        names = ", ".join(f'"{c}"' for c in classes)
        fh.write(f"constexpr const char* kClassNames[kClassCount] = {{{names}}};\n\n")
        emit_array(fh, "kW1", W1)
        emit_array(fh, "kB1", b1)
        emit_array(fh, "kW2", W2)
        emit_array(fh, "kB2", b2)
        emit_array(fh, "kW3", W3)
        emit_array(fh, "kB3", b3)
        fh.write("""/// 一次前向：输入 kFeatureCount 维原始特征（未归一化），输出 logits。
inline void classify(const float* x, float* logits) {
    float h1[kHidden1];
    float h2[kHidden2];
    for (std::size_t o = 0; o < kHidden1; ++o) {
        float acc = kB1[o];
        for (std::size_t i = 0; i < kFeatureCount; ++i) acc += kW1[o * kFeatureCount + i] * x[i];
        h1[o] = acc > 0.0f ? acc : 0.0f;
    }
    for (std::size_t o = 0; o < kHidden2; ++o) {
        float acc = kB2[o];
        for (std::size_t i = 0; i < kHidden1; ++i) acc += kW2[o * kHidden1 + i] * h1[i];
        h2[o] = acc > 0.0f ? acc : 0.0f;
    }
    for (std::size_t o = 0; o < kClassCount; ++o) {
        float acc = kB3[o];
        for (std::size_t i = 0; i < kHidden2; ++i) acc += kW3[o * kHidden2 + i] * h2[i];
        logits[o] = acc;
    }
}

}  // namespace listening_model
""")

    card = {
        "version": metrics.get("version", "v0.1.0"),
        "architecture": metrics.get("model", "ContextNet v0.1 (MLP 92-128-64-9)"),
        "classes": classes,
        "feature_count": int(W1.shape[1]),
        "feature_names": feature_names,
        "parameters": int(W1.size + b1.size + W2.size + b2.size + W3.size + b3.size),
        "weights_bytes_f32": int((W1.size + b1.size + W2.size + b2.size + W3.size + b3.size) * 4),
        "export_check": {
            "numpy_vs_torch_max_abs_diff": max_diff,
            "argmax_identical": same,
            "int8_simulated_agreement": int8_agreement,
        },
        "training_metrics": metrics,
    }
    card_path = Path(args.card) if args.card else header.with_name("model_card.json")
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2))

    if args.vectors:
        n = emit_test_vectors(header.with_name("test_vectors.h"), Path(args.vectors),
                              args.vectors_per_class, int(W1.shape[1]),
                              (W1, b1, W2, b2, W3, b3))
        print(f"自检向量 {n} 条 -> {header.with_name('test_vectors.h')}")

    print(json.dumps(card["export_check"], indent=2))
    print(f"参数 {card['parameters']}（float32 {card['weights_bytes_f32'] / 1024:.1f} KiB）-> {header}")
    if not same:
        raise SystemExit("导出回验失败：argmax 不一致，禁止使用该头文件")


if __name__ == "__main__":
    main()
