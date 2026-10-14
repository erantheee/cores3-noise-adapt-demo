#!/usr/bin/env python3
"""声纹可分性测量——止损门工具。

在投入任何端侧声纹工程之前，先用真实录音回答一个问题：**这个人的声纹分得开吗？**

姊妹项目已经失败过一次：24 频段频谱包络的类内抖动约 0.15，而人际间距约 0.05，
抖动是间距的三倍，不存在能把两组分开的阈值。所以这一次先量再投。

判据在运行前就写死，跑完不许再调：

    类间间距 / 类内抖动 >= 3    且    EER <= 5%     -> 通过，可以继续做端侧门控
    否则                                            -> 停止声纹路线，如实上报分布

用法
----
    python3 tools/voice_separability.py --enroll 甲A.wav 甲B.wav --impostor 乙A.wav 乙B.wav

    或用目录：--enroll-dir 记录/甲 --impostor-dir 记录/乙

需要的录音（48 kHz 或任意采样率单声道 WAV，建议 10 秒/条）：

    同一人、分两天各录 2-3 条    -> 用 --enroll，用来算类内抖动
    另一人录 2-3 条              -> 用 --impostor，用来算类间间距

    录的时候刻意改变条件（距离、音量、有无背景声），因为真实场景就是会漂移。
    只在一个条件下录出来的"稳定"声纹，在真实使用中不成立。

依赖
----
需要 speaker embedding 模型。本工具**不内置模型**，也不假装能算：它检查依赖是否就位，
缺什么就明确报缺什么。可选实现见 --backend。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path

# 判据：运行前写死，不允许跑完再调。
MIN_GAP_RATIO = 3.0
MAX_EER = 0.05

SUPPORTED_SUFFIXES = {".wav"}


class MissingDependency(Exception):
    """依赖未就位。工具必须明说缺什么，而不是给一个假结果。"""


def _require(module: str, hint: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise MissingDependency(f"缺少 {module}。{hint}") from exc


def load_audio(path: Path, target_sr: int = 16000):
    """读单声道 WAV。返回 (采样点列表, 采样率)。

    重采样到 16 kHz：说话人 embedding 模型基本都在 16 kHz 上训练，喂 48 kHz 会得到
    一个"能用但没意义"的相似度分布。
    """
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"{path} 不是 WAV。先转成 WAV 再喂进来，避免解码器差异影响结论。")
    wave = _require("wave", "标准库缺失，环境异常。")
    _require("numpy", "pip install numpy")

    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width != 2:
        raise ValueError(f"{path} 位深是 {width * 8} bit，需要 16 bit。")

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    if rate != target_sr:
        # 线性插值足够：这里只关心 embedding 的可分性，不关心音频质量。
        duration = samples.shape[0] / rate
        target_count = int(round(duration * target_sr))
        source_index = np.linspace(0.0, 1.0, num=samples.shape[0], endpoint=False)
        target_index = np.linspace(0.0, 1.0, num=target_count, endpoint=False)
        samples = np.interp(target_index, source_index, samples).astype(np.float32)
        rate = target_sr

    return samples, rate


def embed(samples, rate: int, backend: str):
    """产出一条单位长度的 speaker embedding。"""
    if backend == "speechbrain":
        _require("torch", "pip install torch")
        _require("speechbrain", "pip install speechbrain")
        raise MissingDependency(
            "speechbrain 后端尚未接线。请在此处接入 ECAPA-TDNN：\n"
            "  from speechbrain.inference.speaker import EncoderClassifier\n"
            "  clf = EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb')\n"
            "  返回 clf.encode_batch(torch.from_numpy(samples)[None, None]).squeeze().numpy()\n"
            "接好之后把本 raise 换成真实实现，并保持返回向量已 L2 归一化。"
        )
    if backend == "esp-sr":
        raise MissingDependency(
            "esp-sr 的声纹模型只跑在 ESP 芯片上，不能用于本机测量。\n"
            "本工具的作用正是**先于**端侧实现判断可分性，所以请用 PC 侧模型（--backend speechbrain）。"
        )
    raise MissingDependency(f"未知后端 {backend!r}。可用：speechbrain。")


def cosine(a, b) -> float:
    import numpy as np  # noqa: PLC0415

    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)


def eer_from_scores(genuine, impostor):
    """等错误率与建议阈值。阈值取使 |FAR-FRR| 最小的点。"""
    thresholds = sorted(set(genuine + impostor))
    if not thresholds:
        return float("nan"), float("nan")
    best = (float("nan"), float("nan"))
    best_delta = float("inf")
    for threshold in thresholds:
        false_reject = sum(1 for s in genuine if s < threshold) / max(len(genuine), 1)
        false_accept = sum(1 for s in impostor if s >= threshold) / max(len(impostor), 1)
        delta = abs(false_reject - false_accept)
        if delta < best_delta:
            best_delta = delta
            best = ((false_reject + false_accept) / 2.0, threshold)
    return best


def describe(values):
    if not values:
        return {"n": 0, "min": None, "median": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "max": ordered[-1],
    }


def collect(pattern_dirs, single_files):
    paths = []
    for directory in pattern_dirs:
        paths.extend(sorted(p for p in Path(directory).glob("*.wav")))
    paths.extend(Path(p) for p in single_files)
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError("找不到这些文件：\n  " + "\n  ".join(missing))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="声纹可分性测量（止损门）")
    parser.add_argument("--enroll", nargs="*", default=[], help="同一人的录音，用来算类内抖动")
    parser.add_argument("--impostor", nargs="*", default=[], help="另一人的录音，用来算类间间距")
    parser.add_argument("--enroll-dir", nargs="*", default=[], help="同上，按目录收集 *.wav")
    parser.add_argument("--impostor-dir", nargs="*", default=[])
    parser.add_argument("--backend", default="speechbrain", help="embedding 后端")
    parser.add_argument("--json", dest="json_out", default=None, help="同时写出机器可读结果")
    args = parser.parse_args()

    try:
        enroll_paths = collect(args.enroll_dir, args.enroll)
        impostor_paths = collect(args.impostor_dir, args.impostor)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if len(enroll_paths) < 2:
        print(
            "错误：同一人至少需要 2 条录音才能算类内抖动。\n"
            "      建议 3 条，而且要**故意改变条件**（距离、音量、有无背景声）——\n"
            "      只在一个条件下录出的'稳定'声纹在真实使用中不成立。",
            file=sys.stderr,
        )
        return 2
    if len(impostor_paths) < 2:
        print("错误：另一人至少需要 2 条录音才能算类间间距。", file=sys.stderr)
        return 2

    try:
        enroll_embeddings = [embed(*load_audio(p), args.backend) for p in enroll_paths]
        impostor_embeddings = [embed(*load_audio(p), args.backend) for p in impostor_paths]
    except MissingDependency as exc:
        print(f"依赖未就位，无法给出结论：\n\n{exc}", file=sys.stderr)
        return 3

    genuine = [cosine(a, b) for a, b in itertools.combinations(enroll_embeddings, 2)]
    impostor = [cosine(a, b) for a in enroll_embeddings for b in impostor_embeddings]

    within = describe(genuine)
    between = describe(impostor)
    # 类内抖动用 1 - 中位相似度，与"间距"同量纲。
    within_jitter = 1.0 - (within["median"] or 0.0)
    between_gap = (within["median"] or 0.0) - (between["median"] or 0.0)
    gap_ratio = (between_gap / within_jitter) if within_jitter > 1e-9 else float("inf")
    eer, threshold = eer_from_scores(genuine, impostor)

    print("=== 声纹可分性 ===")
    print(f"录音：同一人 {len(enroll_paths)} 条，另一人 {len(impostor_paths)} 条")
    print()
    print(f"  类内相似度   n={within['n']:3d}  中位 {within['median']:.4f}  "
          f"范围 {within['min']:.4f}–{within['max']:.4f}")
    print(f"  类间相似度   n={between['n']:3d}  中位 {between['median']:.4f}  "
          f"范围 {between['min']:.4f}–{between['max']:.4f}")
    print()
    print(f"  类内抖动 (1 - 类内中位) = {within_jitter:.4f}")
    print(f"  类间间距 (类内中位 - 类间中位) = {between_gap:.4f}")
    print(f"  间距 / 抖动 = {gap_ratio:.2f}")
    if math.isfinite(eer):
        print(f"  EER = {eer * 100:.2f}%  建议阈值 = {threshold:.4f}")
    print()

    passed_ratio = gap_ratio >= MIN_GAP_RATIO
    passed_eer = math.isfinite(eer) and eer <= MAX_EER
    print(f"  判据（运行前写死）：间距/抖动 >= {MIN_GAP_RATIO}  且  EER <= {MAX_EER * 100:.0f}%")
    print(f"    间距/抖动  {gap_ratio:.2f}   {'通过' if passed_ratio else '不通过'}")
    print(f"    EER        {eer * 100:.2f}%   {'通过' if passed_eer else '不通过'}")
    print()

    if passed_ratio and passed_eer:
        print("  结论：通过。可以进入端侧声纹门控（docs/edge-feasibility.md 第四节）。")
        print("        注意：这类特征量的抖动通常大于人际间距，做之前请确认上表的")
        print("        '范围'一列没有重叠——中位数通过不代表分布能分开。")
    else:
        print("  结论：不通过。按止损门，停止声纹路线并如实上报。")
        print("        退回方案：波束成形 + 决策层，不投模型工程。")
        print("        不要为了让数字变好而调整判据或挑录音。")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "enroll_files": [str(p) for p in enroll_paths],
                    "impostor_files": [str(p) for p in impostor_paths],
                    "within": within,
                    "between": between,
                    "within_jitter": within_jitter,
                    "between_gap": between_gap,
                    "gap_ratio": gap_ratio,
                    "eer": eer,
                    "threshold": threshold,
                    "criteria": {"min_gap_ratio": MIN_GAP_RATIO, "max_eer": MAX_EER},
                    "passed": bool(passed_ratio and passed_eer),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n机器可读结果已写入 {args.json_out}")

    return 0 if (passed_ratio and passed_eer) else 1


if __name__ == "__main__":
    sys.exit(main())
