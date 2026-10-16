#!/usr/bin/env python3
"""配对采集：CoreS3 六轴 100 Hz + Mac 麦克风，同一时间基，按脚本打活动标签。

为什么不能直接用现成的 collect.py：它只存设备 JSON，且明确写了 `raw_audio: False`。
而"我在不在说话"这件事的标签**必须来自设备之外** —— 拿设备自己的 voice_likelihood
当标签是循环论证，那正是要被替代的那一路。

时间基怎么对齐（这是本脚本唯一需要动脑的地方）：

  * 每条串口 JSON 记下 **host 单调时钟到达时刻** `host_mono_ns`，和设备自己的 `t_us`
    一起落盘。两者之间是一个近似线性的映射（USB 串口延迟基本恒定，抖动几十毫秒）。
  * 音频由 ffmpeg 录成 wav。**wav 的 t=0 与 host 时钟的对应关系不靠"进程启动时刻"猜** ——
    那个误差有两三百毫秒，且不可观测。
  * 改用**同步脉冲**：脚本会让你在开始和结束时各拍一下桌子。这一下同时出现在
    音频（咔哒声）和六轴（加速度尖峰）里，事后互相关就能把两条时间轴对到毫秒级。
    两下还可以顺带量出这一段里的时钟漂移。

用法：
    python3 collect_selfvoice.py --port /dev/cu.usbmodem101 --subject p01 --position right-ear

先决条件：
    1. 设备已刷 ear-context 固件并运行（六轴 100 Hz 在推 JSON）
    2. **设备戴在耳朵上** —— 桌面平放采的数据对"我在说话"没有意义
    3. 终端有麦克风权限；首次跑 macOS 会弹窗，允许后重跑
    4. 想跳过录音就加 --no-audio（标签仍会存，但失去了独立的语音真值）
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

# ── 采集脚本 ────────────────────────────────────────────────────────────────
#
# 每一段是 (标签, 秒数, 提示)。标签是我们希望模型学会区分的"轴2：我是否在说话"，
# 后面三段是**负样本**，比正样本更重要 —— 没有它们，模型会把任何头部微动都当成说话。
PROTOCOL: list[tuple[str, float, str]] = [
    ("sync",          0,  "拍一下桌子（同步脉冲 1/2）"),
    ("silent",       20,  "静坐。不说话，不要动"),
    ("self_speech",  25,  "自己说话。朗读或自由说，音量自然"),
    ("silent",       12,  "静坐。不说话"),
    ("fidget",       25,  "抖腿。**不要说话**，上半身尽量别动"),
    ("silent",       12,  "静坐。不说话"),
    ("chew_nod",     25,  "咀嚼或点头。**不要说话**"),
    ("silent",       12,  "静坐。不说话"),
    ("speech_music", 25,  "用手机外放音乐，同时自己说话（最难的一档）"),
    ("sync",          0,  "再拍一下桌子（同步脉冲 2/2）"),
]

# 自检用短流程：只为验证"串口读得到、录音起得来、文件写得下"，不做任何分析。
QUICK_PROTOCOL: list[tuple[str, float, str]] = [
    ("sync",         0, "拍一下桌子（自检）"),
    ("silent",       4, "静坐，不说话"),
    ("self_speech",  6, "说几句话"),
    ("sync",         0, "再拍一下桌子（自检结束）"),
]


class SerialRecorder(threading.Thread):
    """读串口 JSON，逐条落盘，同时记 host 到达时刻。"""

    def __init__(self, port_name: str, sink_path: Path):
        super().__init__(daemon=True)
        self.port_name = port_name
        self.sink_path = sink_path
        self.stop_flag = threading.Event()
        self.counts = {"imu": 0, "context": 0}
        self.other_lines: list[str] = []
        self.resets = 0
        self.error: str | None = None
        self.first_host_mono_ns: int | None = None
        self.last_host_mono_ns: int | None = None

    def run(self) -> None:
        import serial

        try:
            port = serial.Serial(self.port_name, 115200, timeout=0.5)
        except Exception as exc:  # noqa: BLE001 - 串口打不开要把原因原样带出去
            self.error = f"{type(exc).__name__}: {exc}"
            return

        previous: dict[str, int] = {}
        try:
            with self.sink_path.open("x", buffering=1, encoding="utf-8") as sink:
                while not self.stop_flag.is_set():
                    line = port.readline()
                    mono = time.monotonic_ns()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        record = None
                    if not (isinstance(record, dict) and record.get("type") in self.counts
                            and isinstance(record.get("t_us"), int)):
                        # 启动横幅、ESP_LOG 之类，留一份方便事后对时间
                        if len(self.other_lines) < 400:
                            self.other_lines.append(
                                line.decode("utf-8", "replace").rstrip())
                        continue
                    kind = record["type"]
                    if kind in previous and record["t_us"] < previous[kind]:
                        self.resets += 1
                        previous.clear()
                    previous[kind] = record["t_us"]
                    # 两个时间戳都留：host_mono_ns 用来和音频对齐，
                    # host_unix_ns 用来和外部记录（比如手机录音）对齐。
                    record["host_mono_ns"] = mono
                    record["host_unix_ns"] = time.time_ns()
                    record["boot_segment"] = self.resets
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self.counts[kind] += 1
                    if self.first_host_mono_ns is None:
                        self.first_host_mono_ns = mono
                    self.last_host_mono_ns = mono
        finally:
            port.close()


class AudioRecorder:
    """用 ffmpeg 录 Mac 默认输入设备；失败不致命，但要显式报出来。

    中间产物是**裸 PCM** 而不是 wav：wav 的头要等 ffmpeg 退出时才写，录制过程中读不到；
    裸 PCM 可以直接读文件尾部算实时电平 —— 这是采集时唯一的合规性反馈。
    "哪一段没出声"必须当场看得见，否则一整轮采完才发现某段没照做。
    结束时再包成标准 wav。
    """

    RATE = 48000
    BYTES_PER_SAMPLE = 2

    def __init__(self, out_dir: Path, device: str):
        self.out_dir = out_dir
        self.pcm_path = out_dir / "audio.pcm"
        self.wav_path = out_dir / "audio.wav"
        self.device = device
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None

    def start(self) -> None:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-f", "avfoundation", "-i", self.device,
            "-ac", "1", "-ar", str(self.RATE), "-f", "s16le",
            "-y", str(self.pcm_path),
        ]
        try:
            # stdin 留着发 'q'，让 ffmpeg 干净退出。
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError:
            self.error = "找不到 ffmpeg"
            return
        time.sleep(1.5)
        if self.proc.poll() is not None:
            err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            self.error = err.splitlines()[-1] if err else "ffmpeg 立刻退出"
            self.proc = None

    def level(self, window_s: float = 0.25) -> float:
        """最近一小段的 RMS，用来在倒计时上显示实时电平。"""
        nbytes = int(window_s * self.RATE) * self.BYTES_PER_SAMPLE
        try:
            size = self.pcm_path.stat().st_size
            if size < nbytes * 2:
                return 0.0
            with self.pcm_path.open("rb") as fh:
                fh.seek(size - nbytes)
                raw = fh.read(nbytes)
            s = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
            return float(np.sqrt((s ** 2).mean())) if s.size else 0.0
        except OSError:
            return 0.0

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 - 兜底强杀，别让采集卡在收尾
                self.proc.kill()
        # 裸 PCM 包成 wav，并删掉中间文件
        try:
            import wave

            if self.pcm_path.exists() and self.pcm_path.stat().st_size > 0:
                with wave.open(str(self.wav_path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(self.BYTES_PER_SAMPLE)
                    w.setframerate(self.RATE)
                    w.writeframes(self.pcm_path.read_bytes())
                self.pcm_path.unlink()
        except Exception as exc:  # noqa: BLE001
            self.error = self.error or f"wav 封装失败：{exc}"


def countdown(seconds: float, instruction: str, index: int, total: int,
              audio: AudioRecorder | None = None) -> tuple[int, int, float]:
    """跑一段提示，返回 (start_ns, end_ns, 本段音频 RMS)。"""
    if seconds <= 0:
        print(f"\n[{index}/{total}] {instruction}")
        try:
            input("       准备好后按回车继续 …")
        except EOFError:      # 无人值守/管道输入下不要卡住
            pass
        now = time.monotonic_ns()
        return now, now, 0.0
    print(f"\n[{index}/{total}] {instruction}  —— {seconds:.0f} 秒")
    start_ns = time.monotonic_ns()
    end = time.monotonic() + seconds
    levels: list[float] = []
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        bar = "█" * int((seconds - left) / seconds * 30)
        # 实时电平：说话段必须看得见柱子，静默段应该是空的 ——
        # 这是采集当时唯一能发现"这一段没照做"的反馈。
        lv = audio.level() if audio else 0.0
        levels.append(lv)
        mark = "▁▂▃▄▅▆▇█"[min(7, int(lv * 400))]
        print(f"\r        {left:4.1f}s {bar:<30} 声{mark} {lv:.4f}", end="", flush=True)
        time.sleep(0.1)
    end_ns = time.monotonic_ns()
    mean = float(np.mean(levels)) if levels else 0.0
    print(f"\r        完成   本段音频 RMS {mean:.5f}{' ' * 30}")
    return start_ns, end_ns, mean


def main() -> int:
    ap = argparse.ArgumentParser(description="配对采集：六轴 + 麦克风 + 活动标签")
    ap.add_argument("--port", required=True)
    ap.add_argument("--subject", required=True, help="匿名被试编号")
    ap.add_argument("--position", default="right-ear",
                    choices=["left-ear", "right-ear", "headband", "desk"])
    ap.add_argument("--audio-device", default=":0",
                    help="ffmpeg avfoundation 输入，默认 :0（系统默认麦克风）")
    ap.add_argument("--no-audio", action="store_true", help="跳过录音")
    ap.add_argument("--quick", action="store_true",
                    help="跑 12 秒短流程，只验证链路通不通，不做分析")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    protocol = QUICK_PROTOCOL if args.quick else PROTOCOL

    if shutil.which("ffmpeg") is None and not args.no_audio:
        print("找不到 ffmpeg；用 --no-audio 也能采，但会失去独立的语音真值。", file=sys.stderr)
        return 2
    if args.position == "desk":
        print("警告：桌面平放采的数据对『我在说话』没有意义 —— 请戴在耳朵上。", file=sys.stderr)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or (Path(__file__).resolve().parent.parent
                       / "listening-model/data/real" / f"{stamp}-selfvoice")
    out.mkdir(parents=True, exist_ok=False)
    print(f"输出目录：{out}")

    audio = AudioRecorder(out, args.audio_device)
    ser = SerialRecorder(args.port, out / "samples.jsonl")

    if not args.no_audio:
        audio.start()
        if audio.error:
            print(f"⚠️  录音没起来：{audio.error}", file=sys.stderr)
            print("    多数情况是终端没有麦克风权限 —— 系统设置 → 隐私与安全性 → 麦克风。",
                  file=sys.stderr)
            print("    本次继续采，但 audio.wav 不会有内容。\n", file=sys.stderr)

    ser.start()
    time.sleep(1.5)
    if ser.error:
        print(f"打不开串口 {args.port}: {ser.error}", file=sys.stderr)
        audio.stop()
        return 2

    t_start_mono = time.monotonic()
    labels: list[dict] = []
    print("\n开始采集。全程保持设备在耳朵上，不要碰它。")
    try:
        for i, (label, secs, instruction) in enumerate(protocol, start=1):
            start_ns, end_ns, seg_rms = countdown(secs, instruction, i, len(protocol),
                                                  audio if not args.no_audio else None)
            if label == "sync":
                continue
            labels.append({
                "label": label,
                "instruction": instruction,
                "start_mono_ns": start_ns,
                "end_mono_ns": end_ns,
                "audio_rms": round(seg_rms, 6),
            })
    except KeyboardInterrupt:
        print("\n用户中断，仍会保存已采到的部分。")
    finally:
        audio.stop()
        ser.stop_flag.set()
        ser.join(timeout=5)

    elapsed = time.monotonic() - t_start_mono
    with (out / "labels.jsonl").open("w", encoding="utf-8") as fh:
        for row in labels:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    (out / "session.json").write_text(json.dumps({
        "schema": 2,
        "started_utc": stamp,
        "subject": args.subject,
        "position": args.position,
        "firmware": "ear-context",
        "purpose": "self-voice (axis 2) separability probe from IMU only",
        "axis_frame": "BMI270 sensor axes; mounting orientation not calibrated",
        "accel_units": "m/s^2 including gravity",
        "gyro_units": "degrees/s",
        "device_time": "monotonic read-completion time on device, not exact acquisition",
        "alignment": {
            "method": "host_mono_ns on each serial line + two table-tap sync pulses",
            "note": "audio wav t=0 is NOT trusted; align by cross-correlating the taps",
        },
        "raw_audio": not args.no_audio and audio.error is None,
        "audio_device": args.audio_device,
        "human_activity_label": "per-segment, see labels.jsonl",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 速率必须用**采样覆盖的时间跨度**算，不能用流程耗时：
    # 串口线程比流程早起 1.5 秒，拿流程耗时当分母会把速率算高一成多（自检时量到 114.3 Hz，
    # 实际是 100.5 Hz）。
    span = ((ser.last_host_mono_ns - ser.first_host_mono_ns) / 1e9
            if ser.first_host_mono_ns and ser.last_host_mono_ns else 0.0)
    rate = ser.counts["imu"] / span if span > 0 else 0.0

    with (out / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump({
            "counts": ser.counts,
            "resets": ser.resets,
            "elapsed_seconds": round(elapsed, 2),
            "sample_span_seconds": round(span, 2),
            "segments": len(labels),
            "audio_error": audio.error,
            "imu_rate_hz": round(rate, 1),
            "other_line_count": len(ser.other_lines),
        }, fh, indent=2)

    print(f"\n采集结束：六轴 {ser.counts['imu']} 条 / 声音帧 {ser.counts['context']} 条 / "
          f"{elapsed:.1f} 秒")
    if span > 0:
        print(f"六轴实测速率 {rate:.1f} Hz（目标 100，按采样跨度 {span:.1f} s 算）")

    # ── 当场合规性小结 ──
    # 上一轮就是这么栽的：speech_music 那一段音频 RMS 只有静默基线的 0.22 倍，也就是
    # 根本没说话；而它被当成正类喂进去，直接把分类器整体反向（汇总 AUC 0.051）。
    # 这件事必须在采完当场就看见，不能等分析。
    if labels and any("audio_rms" in r for r in labels):
        silent = [r["audio_rms"] for r in labels if r["label"] == "silent"]
        base = float(np.mean(silent)) if silent else 0.0
        print("\n逐段音频电平（合规性自检）")
        for r in labels:
            v = r["audio_rms"]
            ratio = v / base if base > 0 else 0.0
            note = ""
            if r["label"] in ("self_speech", "speech_music") and ratio < 1.5:
                note = "  ⚠️ 这一段应该在说话，电平却接近静默 —— 多半没照做"
            elif r["label"] == "silent" and ratio > 3:
                note = "  ⚠️ 静默段有明显声音"
            print(f"  {r['label']:<14}{v:>10.5f}  {ratio:>5.2f}× 静默基线{note}")
        if base <= 0:
            print("  ⚠️ 静默基线为 0：录音可能整段没起来")

    print(f"→ {out}")
    print("下一步：python3 selfvoice_report.py --session <上面这个目录>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
