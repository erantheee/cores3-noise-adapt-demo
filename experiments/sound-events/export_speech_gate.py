"""训练"说话 vs 非说话"二分类学生，并导出成固件可编译的 C 头文件。

输入数据（都是 92 维、2 s 窗，与端侧 listen-model 输入同口径）：
  * multi 会话：官方 YAMNet 的 speech>0.5 作为标签；
  * TTS 会话：官方 YAMNet 的 speech>0.5 作为标签（4 个音色，补正样本多样性）；
  * B 会话：人工分段标签（真人说话）。
学生结构固定 92 → 24 → 2（ReLU），约 2.3k 参数，端侧推理微秒级。

导出时把训练集的均值/标准差折进第一层，端侧直接吃未归一化的 92 维原始特征：
    z1 = (W1/sd) x + (b1 - W1·(mu/sd))
    h  = relu(z1)
    logits = W2 h + b2
同时生成 `test/speech_gate_golden.h`，用两个固定输入把 Python 输出钉成金标准。

用法：
    .venv/bin/python experiments/sound-events/export_speech_gate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments" / "listening-model" / "scripts"))
import features as listening_features  # noqa: E402

FEATURE_COUNT = 92
HIDDEN = 24
#: 这些维依赖 `broadband_dbfs`（设备用 48 kHz 时域 RMS，训练数据里没有这个字段，
#: 早期脚本用 24 段功率重算的近似值），端侧运行时与训练侧不一致，必须两端都清零：
#: 72–75 = 电平 mean/std/min/max，76–79 = voice_likelihood mean/std/active/transitions，
#: 84 = 电平阶跃次数。只保留 24 段本身的统计量与频带派生的描述量。
MASKED_INDICES = tuple(range(72, 80)) + (84,)
CLASS_COUNT = 2
MULTI_DIR = ROOT / "记录" / "教师数据" / "20261019-151706-multi"
TTS_DIR = ROOT / "记录" / "教师数据" / "20261019-160303-tts"
B_CSV = ROOT / "记录" / "键盘风声" / "20261019-142748-speech" / "live.csv"


def windows_from_bands(bands: np.ndarray) -> np.ndarray:
    feats = listening_features.voice_features_from_bands(bands)
    power = np.power(10.0, bands / 10.0)
    broadband = 10.0 * np.log10(np.maximum(power.sum(axis=-1), 1e-12))
    likelihood = listening_features.voice_likelihood(bands, broadband, feats)
    frames = {
        "t_s": np.arange(len(bands)) * 0.1,
        "broadband_dbfs": broadband,
        "band_dbfs": bands,
        "voice_likelihood": likelihood,
        "mic_valid": np.ones(len(bands), bool),
        **feats,
    }
    return np.array([
        listening_features.window_vector(frames, start, start + listening_features.WINDOW_FRAMES,
                                         motion=None, when=None)
        for start in range(0, len(bands) - listening_features.WINDOW_FRAMES + 1, 5)
    ])


def teacher_session(directory: Path):
    frames = [json.loads(line) for line in
              (directory / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line]
    bands = np.array([frame["bands"] for frame in frames], dtype=np.float64)
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    first_chunk = meta["first_chunk_ts_ms"]
    frame_time = np.array([(frame["t_ms"] - first_chunk) / 1000.0 for frame in frames])
    teacher = [json.loads(line) for line in
               (directory / "teacher_yamnet.jsonl").read_text(encoding="utf-8").splitlines() if line]
    teacher_time = np.array([row["center_s"] for row in teacher])
    speech = np.array([row["targets"].get("speech", 0.0) for row in teacher])
    labels = np.array([1 if speech[int(np.argmin(np.abs(teacher_time - t)))] > 0.5 else 0
                       for t in frame_time])
    windows = windows_from_bands(bands)
    window_labels = np.array([
        int(labels[min(len(labels) - 1, i * 5 + 10 - 10):min(len(labels), i * 5 + 10) + 1].mean() > 0.5)
        for i in range(len(windows))
    ])
    return windows, window_labels


def human_session(csv_path: Path):
    import csv
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    bands = np.array([[float(row[f"band_{band:02d}"]) for band in range(24)] for row in rows])
    windows = windows_from_bands(bands)
    labels = []
    for i in range(len(windows)):
        index = min(len(rows) - 1, i * 5 + 10)
        segment = [1 if rows[j]["phase"] == "speech" else 0
                   for j in range(max(0, index - 10), index + 1)]
        labels.append(int(np.mean(segment) > 0.5))
    return windows, np.array(labels)


def train(X: np.ndarray, y: np.ndarray):
    import torch
    import torch.nn as nn

    torch.manual_seed(7)
    mean = X.mean(axis=0)
    std = np.maximum(X.std(axis=0), 1e-6)
    positive_weight = (len(y) - y.sum()) / max(y.sum(), 1)
    model = nn.Sequential(nn.Linear(FEATURE_COUNT, HIDDEN), nn.ReLU(),
                          nn.Linear(HIDDEN, CLASS_COUNT))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.005, weight_decay=1e-3)
    loss_fn = nn.CrossEntropyLoss(weight=torch.tensor([1.0, positive_weight],
                                                      dtype=torch.float32))
    inputs = torch.tensor((X - mean) / std, dtype=torch.float32)
    targets = torch.tensor(y.astype(np.int64))
    for _ in range(1500):
        optimizer.zero_grad()
        loss_fn(model(inputs), targets).backward()
        optimizer.step()
    model.eval()
    first, second = model[0], model[2]
    w1 = first.weight.detach().numpy() / std[None, :]
    b1 = first.bias.detach().numpy() - (first.weight.detach().numpy() @ (mean / std))
    w2 = second.weight.detach().numpy()
    b2 = second.bias.detach().numpy()
    return w1, b1, w2, b2


def infer(x: np.ndarray, w1, b1, w2, b2):
    hidden = np.maximum(w1 @ x + b1, 0.0)
    logits = w2 @ hidden + b2
    logits = logits - logits.max()
    prob = np.exp(logits) / np.exp(logits).sum()
    return prob[1]


def emit_one(value: float) -> str:
    text = f"{float(value):.9g}"
    if "." not in text and "e" not in text and "E" not in text:
        text += ".0"
    return text + "f"


def emit_float(values: np.ndarray) -> str:
    tokens = []
    for value in values.ravel():
        text = f"{float(value):.9g}"
        if "." not in text and "e" not in text and "E" not in text:
            text += ".0"  # C++ 里 `0f` 是非法的（0 后跟 f 会被当成八进制后缀）
        tokens.append(text + "f")
    return ",".join(tokens)


def write_header(w1, b1, w2, b2) -> None:
    lines = [
        "// 由 experiments/sound-events/export_speech_gate.py 自动生成，请勿手改。",
        "// 模型: SpeechGate (MLP 92-24-2)，输入与 main/listening_model.h 相同的 2 s 窗特征。",
        "// 归一化已折进第一层；输出 logits[2]，class 1 = 有人在说话。",
        "#pragma once",
        "",
        "#include <cstddef>",
        "",
        "namespace speech_gate {",
        "",
        f"constexpr std::size_t kFeatureCount = {FEATURE_COUNT};",
        f"constexpr std::size_t kHidden = {HIDDEN};",
        f"constexpr std::size_t kClassCount = {CLASS_COUNT};",
        "",
        f"constexpr float kW1[{HIDDEN * FEATURE_COUNT}] = {{{emit_float(w1)}}};",
        "",
        f"constexpr float kB1[{HIDDEN}] = {{{emit_float(b1)}}};",
        "",
        f"constexpr float kW2[{CLASS_COUNT * HIDDEN}] = {{{emit_float(w2)}}};",
        "",
        f"constexpr float kB2[{CLASS_COUNT}] = {{{emit_float(b2)}}};",
        "",
        "/// 一次前向：输入 92 维原始特征，输出 2 个 logits（class 1 = speech）。",
        "/// 训练/推理都要屏蔽依赖设备 broadband 的维（见 export_speech_gate.py 的 MASKED_INDICES）。",
        "inline void classify(const float* x, float* logits) {",
        "    float masked[kFeatureCount];",
        "    for (std::size_t i = 0; i < kFeatureCount; ++i) masked[i] = x[i];",
        "    for (std::size_t i = 72; i < 80; ++i) masked[i] = 0.0f;",
        "    masked[84] = 0.0f;",
        "    float hidden[kHidden];",
        "    for (std::size_t o = 0; o < kHidden; ++o) {",
        "        float acc = kB1[o];",
        "        for (std::size_t i = 0; i < kFeatureCount; ++i) acc += kW1[o * kFeatureCount + i] * masked[i];",
        "        hidden[o] = acc > 0.0f ? acc : 0.0f;",
        "    }",
        "    for (std::size_t o = 0; o < kClassCount; ++o) {",
        "        float acc = kB2[o];",
        "        for (std::size_t i = 0; i < kHidden; ++i) acc += kW2[o * kHidden + i] * hidden[i];",
        "        logits[o] = acc;",
        "    }",
        "}",
        "",
        "}  // namespace speech_gate",
        "",
    ]
    (ROOT / "main" / "speech_gate_model.h").write_text("\n".join(lines), encoding="utf-8")


def write_golden(w1, b1, w2, b2) -> None:
    rng = np.random.default_rng(11)
    inputs = [
        np.zeros(FEATURE_COUNT),
        np.linspace(-1.0, 1.0, FEATURE_COUNT),
        rng.normal(0.0, 1.0, FEATURE_COUNT),
    ]
    for values in inputs:
        for index in MASKED_INDICES:
            values[index] = 0.0
    probs = [infer(x, w1, b1, w2, b2) for x in inputs]
    lines = [
        "// 由 experiments/sound-events/export_speech_gate.py 自动生成，请勿手改。",
        "// SpeechGate 的固定输入金标准：Python 侧概率（class 1）。",
        "#pragma once",
        "",
        "#include <cstddef>",
        "",
        "namespace speech_gate_golden {",
        "",
        f"constexpr std::size_t kSampleCount = {len(inputs)};",
        f"constexpr std::size_t kFeatureCount = {FEATURE_COUNT};",
        "",
    ]
    for index, values in enumerate(inputs):
        lines.append(f"constexpr float kInput{index}[kFeatureCount] = {{{emit_float(values)}}};")
    lines.append("")
    lines.append("constexpr float kExpectedProb[kSampleCount] = {"
                 + ",".join(emit_one(prob) for prob in probs) + "};")
    lines.append("")
    lines.append("constexpr const float* kInputs[kSampleCount] = {kInput0, kInput1, kInput2};")
    lines.append("")
    lines.append("}  // namespace speech_gate_golden")
    lines.append("")
    (ROOT / "test" / "speech_gate_golden.h").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    multi_x, multi_y = teacher_session(MULTI_DIR)
    tts_x, tts_y = teacher_session(TTS_DIR)
    b_x, b_y = human_session(B_CSV)
    X = np.concatenate([multi_x, tts_x, b_x])
    y = np.concatenate([multi_y, tts_y, b_y])
    for index in MASKED_INDICES:
        X[:, index] = 0.0
    print(f"训练样本 {len(y)}：positive {int(y.sum())} / {len(y)}")
    w1, b1, w2, b2 = train(X, y)
    write_header(w1, b1, w2, b2)
    write_golden(w1, b1, w2, b2)
    print("-> main/speech_gate_model.h, test/speech_gate_golden.h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
