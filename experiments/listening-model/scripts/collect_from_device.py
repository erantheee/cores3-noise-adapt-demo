#!/usr/bin/env python3
"""从 CoreS3 采集带标签的真实环境数据，并转成训练数据集。

采集（设备每 100 ms 输出一行 earbud_command JSON，含 24 频段环境谱）：
    <IDF 的 python> scripts/collect_from_device.py --port /dev/cu.usbmodem101 \
        --label office --seconds 60 --out data/real/office-1

转换（多个会话目录 -> 一个 npz）：
    <IDF 的 python> scripts/collect_from_device.py --convert data/real --out artifacts/dataset_real.npz

为什么用设备自己输出的频段值而不是重新分析录音：设备 JSON 里的 24 个频段就是固件算出来的
特征（同一段代码在推理时也用这些值），用它训练等于"特征零错位"。录音通道是 16 kHz 的
降采样流，只用来人工复核和低频段交叉验证。

两处已知近似（都属于"JSON 里暂时没有这个字段"，上固件时都只是一行 printf）：
  * broadband_dbfs：用 24 频段功率和近似（带限 RMS，缺 14 kHz 以上与 20 Hz 以下的能量）
  * motion：固定 0（IMU 值当前不在 JSON 里）

因此用本工具产出的数据训练出的模型，在固件里落地的第一步应当是：把这两个字段补进 JSON，
再用同一条管线重训一次，消掉近似。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import (BAND_COUNT, FEATURE_NAMES, VOCAL_BANDS, WINDOW_FRAMES,  # noqa: E402
                      window_vector)


def frames_from_bands(band_dbfs: np.ndarray) -> dict:
    """把逐帧频段值补齐成 window_vector 需要的结构（描述量是频段的确定性函数，可精确重构）。"""
    import features as F

    feats = F.voice_features_from_bands(band_dbfs)
    power = np.power(10.0, band_dbfs / 10.0)
    broadband = 10.0 * np.log10(np.maximum(power.sum(axis=-1), 1e-12))
    vl = F.voice_likelihood(band_dbfs, broadband, feats)
    n = band_dbfs.shape[0]
    return {
        "t_s": np.arange(n) * 0.1,
        "broadband_dbfs": broadband,
        "band_dbfs": band_dbfs,
        "voice_likelihood": vl,
        "mic_valid": np.ones(n, dtype=bool),
        **feats,
    }


def convert(root: Path, out: Path, hop: int = 5, min_frames: int = WINDOW_FRAMES):
    sessions = sorted(d for d in root.iterdir() if (d / "frames.jsonl").exists())
    if not sessions:
        raise SystemExit(f"{root} 下没有找到 <label>/frames.jsonl")
    X, y, meta, classes = [], [], [], []
    for sess in sessions:
        label = sess.name.split("-")[0]
        if label not in classes:
            classes.append(label)
        ci = classes.index(label)
        rows = [json.loads(l) for l in (sess / "frames.jsonl").read_text().splitlines() if l.strip()]
        bands = np.array([r["ambient_band_dbfs"] for r in rows], dtype=np.float64)
        if bands.shape[1] != BAND_COUNT:
            raise SystemExit(f"{sess}: 频段数 {bands.shape[1]} != {BAND_COUNT}")
        frames = frames_from_bands(bands)
        n = len(rows)
        starts = range(0, max(n - min_frames + 1, 1), hop)
        for s in starts:
            X.append(window_vector(frames, s, s + WINDOW_FRAMES, motion=None, when=None))
            y.append(ci)
            meta.append(f"{sess.name}@{s * 0.1:.1f}s")
    X = np.asarray(X, dtype=np.float32)
    y = np.asarray(y, dtype=np.int64)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, X_train=X, y_train=y, X_val=X[:0], y_val=y[:0],
                        classes=np.array(classes), feature_names=np.array(FEATURE_NAMES),
                        mu=X.mean(axis=0), sigma=np.maximum(X.std(axis=0), 1e-6),
                        meta_train=np.array(meta), meta_val=np.array([], dtype=object))
    print(f"{len(X)} 个窗口，类别 {classes} -> {out}")


def capture(port: str, baud: int, label: str, seconds: float, out_dir: Path, wav: bool):
    import serial

    out_dir.mkdir(parents=True, exist_ok=True)
    frames_path = out_dir / "frames.jsonl"
    wav_path = out_dir / "audio.wav"
    writer = wave.open(str(wav_path), "wb") if wav else None
    if writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
    n_frames = n_chunks = 0
    finished = False
    with serial.Serial(port=port, baudrate=baud, timeout=0.3) as ser, \
            frames_path.open("w") as fh:
        ser.dtr = True
        ser.rts = True
        if wav:
            ser.write(b"rec start %d\n" % int(seconds))
            ser.flush()
        deadline = time.time() + seconds + 5.0
        while time.time() < deadline and not finished:
            line = ser.readline()
            if not line:
                continue
            try:
                payload = json.loads(line.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "earbud_command" and "ambient_band_dbfs" in payload:
                fh.write(json.dumps({
                    "t_ms": payload.get("timestamp_ms"),
                    "band_dbfs": payload["ambient_band_dbfs"],
                    "mode": payload.get("mode"),
                    "environment": payload.get("environment"),
                    "confidence": payload.get("confidence"),
                }, ensure_ascii=False) + "\n")
                fh.flush()
                n_frames += 1
            elif kind == "mic_chunk" and writer is not None:
                pcm = base64.b64decode(payload["pcm"])
                writer.writeframes(pcm)
                n_chunks += 1
            elif kind == "capture" and payload.get("state") == "finished":
                finished = True
    if writer:
        writer.close()
    (out_dir / "meta.json").write_text(json.dumps({
        "label": label, "seconds": seconds,
        "frames": n_frames, "mic_chunks": n_chunks,
        "note": "broadband 用频段和近似；motion 固定 0（见脚本头部说明）",
    }, ensure_ascii=False, indent=2))
    print(f"{n_frames} 帧 JSON、{n_chunks} 个音频块 -> {out_dir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--label")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--out")
    ap.add_argument("--no-wav", action="store_true", help="不录 16 kHz 音频")
    ap.add_argument("--convert", help="转换模式：目录（内含 <label>/frames.jsonl）")
    args = ap.parse_args()
    if args.convert:
        convert(Path(args.convert), Path(args.out))
    else:
        if not (args.port and args.label and args.out):
            sys.exit("采集模式需要 --port / --label / --out")
        capture(args.port, args.baud, args.label, args.seconds, Path(args.out),
                not args.no_wav)


if __name__ == "__main__":
    main()
