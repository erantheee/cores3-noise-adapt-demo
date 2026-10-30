"""从在位上位机的 /api/snapshot 采集设备 24 段环境谱（不抢串口）。

为什么走 HTTP 而不是直连串口：上位机已经独占 /dev/cu.usbmodem101；再开一次串口会把
两边的数据都读坏（README「真机踩到的坑」第 16 项）。`/api/snapshot` 返回的是上位机刚解析
出来的同一帧，且只读、不改变设备状态。

样本口径（与模型输入一致，不是另算的近似）：

* 一行 = 设备的一帧（10 Hz）。同一 `sequence` 只落一次；轮询快于设备节拍即可无遗漏。
* 24 个值是 `frame.ambient_band_dbfs`，就是 92 维窗特征里的 `band_*` 来源。
* 同帧记下模型当时的输出（`context` / `context_score` / `context_valid`）与环境四分类，
  这样离线分析可以直接问「模型在键盘上到底输出了什么」，不用回放。

标签由时间分区给出（`--script`），可用 `say` / 蜂鸣在换段时提示操作者。提示音本身也会
进麦克风，所以每段开头记为 `cue=1`，分析时必须剔除（默认剔 2 s，见 `--guard`）。

用法：

    # 最简：采 60 秒，不提示，自己掐表
    python3 tools/live_band_capture.py --seconds 60 --out 记录/键盘风声/run.csv

    # 带提示：静置 10s → 敲键盘 15s → 停 6s → 风声 15s → 停 6s
    python3 tools/live_band_capture.py --out 记录/键盘风声/run.csv \
        --script quiet:10,keyboard:15,pause:6,wind:15,pause:6 --cue say

    # 掩蔽矩阵的可听性验收（2026-10-30）：看 applied_* / masking_* / out_clip_frames
    python3 tools/live_band_capture.py --out 记录/eq验收/20261030-a.csv \
        --script song_only:30,song_noise:45,song_talk:30 --cue say

输出 CSV 列：wall_iso, elapsed_s, phase, cue, seq, device_ts_ms, bands 00..23,
context, context_score, context_valid, event, event_valid, environment, state, mode,
conversation, speech_gain_db, media_percent, own_voice_confidence, imu_motion,
ambient_open, execution_se（system EQ 侧：available/execution/applied/masking/削波/余量）。

退出码：0 正常；1 上位机不可达/帧里没有频谱字段（这条必须报错，不能写出空数据当成功）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8787/api/snapshot"
BAND_COUNT = 24

CSV_FIELDS = (
    ["wall_iso", "elapsed_s", "phase", "cue", "seq", "device_ts_ms"]
    + [f"band_{i:02d}" for i in range(BAND_COUNT)]
    + [
        "context",
        "context_score",
        "context_valid",
        "event",
        "event_valid",
        "environment",
        "state",
        "mode",
        "conversation",
        "speech_gain_db",
        "media_percent",
        "own_voice_confidence",
        "imu_motion",
        "ambient_open",
        # system EQ 执行端（2026-10-30 掩蔽矩阵验收）：决策在 S3、动作在 Mac，
        # 所以"设备说了什么"与"歌被改成了什么"必须同帧落盘，否则事后对不上。
        "se_available",
        "se_execution",
        "applied_max_db",
        "applied_peak_abs_db",
        "applied_bands_active",
        "masking_matrix_enabled",
        "masking_peak_db",
        "masking_active_bands",
        "masking_exposure",
        "content_transient_risk",
        "content_harshness",
        "out_peak",
        "out_clip_frames",
        "headroom_gain",
    ]
)


def parse_script(text: str) -> list[tuple[str, float]]:
    """`quiet:10,keyboard:15` -> [("quiet", 10.0), ...]。段名原样保留，方便事后 grep。"""
    if not text:
        return []
    phases: list[tuple[str, float]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, seconds = item.partition(":")
        try:
            length = float(seconds)
        except ValueError as exc:
            raise SystemExit(f"--script 里这一段没有秒数：{item!r}") from exc
        if length <= 0:
            raise SystemExit(f"--script 里这一段秒数必须 > 0：{item!r}")
        phases.append((name.strip() or f"phase{len(phases)}", length))
    return phases


def total_seconds(phases: list[tuple[str, float]], fallback: float) -> float:
    if not phases:
        return fallback
    return sum(length for _, length in phases)


def _num(value) -> float | None:
    """CSV/快照里的数是字符串或 None，统一成 float 或 None（不把 0 当缺失）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value, digits: int = 2) -> str:
    number = _num(value)
    return "" if number is None else f"{number:.{digits}f}"


def percentile(values: list[float | None], fraction: float) -> float | None:
    """线性插值分位数。不引 numpy：与 host 自测同一纪律（纯标准库、可离线复算）。"""
    data = sorted(value for value in values if value is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(data) - 1)
    weight = position - low
    return data[low] * (1 - weight) + data[high] * weight


def systemeq_columns(snapshot: dict) -> dict:
    """system EQ 执行端的实测列。缺字段一律留空，不写 0——"没测到"不是"测到 0"。"""
    side = snapshot.get("systemeq") or {}
    applied = side.get("applied_bands")
    values = [float(value) for value in applied] if isinstance(applied, list) else []
    positive = [value for value in values if value > 0]
    return {
        "se_available": side.get("available"),
        "se_execution": side.get("execution"),
        "applied_max_db": f"{max(positive):.2f}" if positive else "",
        "applied_peak_abs_db": f"{max(abs(value) for value in values):.2f}" if values else "",
        "applied_bands_active": sum(1 for value in values if value > 0.05) if values else "",
        "masking_matrix_enabled": side.get("masking_matrix_enabled"),
        "masking_peak_db": _fmt(side.get("masking_peak_db")),
        "masking_active_bands": side.get("masking_active_bands"),
        "masking_exposure": _fmt(side.get("masking_exposure"), 3),
        "content_transient_risk": _fmt(side.get("content_transient_risk"), 3),
        "content_harshness": _fmt(side.get("content_harshness"), 3),
        "out_peak": _fmt(side.get("out_peak"), 4),
        "out_clip_frames": side.get("out_clip_frames"),
        "headroom_gain": _fmt(side.get("output_headroom_gain"), 4),
    }


def print_summary(rows: list[dict], gaps: int, threshold_db: float = 2.0) -> None:
    """分阶段摘要。验收判据就写在这里，避免事后从 CSV 里挑好看的帧。"""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if str(row.get("cue")) == "1":
            continue
        groups.setdefault(str(row.get("phase")), []).append(row)
    if not groups:
        return
    print("\n分阶段摘要（剔除 cue 段；applied_max = systemeq.applied_bands 的正向峰值）", flush=True)
    for phase, items in groups.items():
        applied = [_num(item.get("applied_max_db")) for item in items]
        applied = [value for value in applied if value is not None]
        peak = [_num(item.get("masking_peak_db")) for item in items]
        clip = [_num(item.get("out_clip_frames")) for item in items]
        clip = [value for value in clip if value is not None]
        headroom = [_num(item.get("headroom_gain")) for item in items]
        headroom_db = [20 * math.log10(value) for value in headroom if value and value > 0]
        active = [_num(item.get("applied_bands_active")) for item in items]
        active = [value for value in active if value is not None]
        share = (sum(1 for value in applied if value >= threshold_db) / len(applied)) if applied else 0.0
        clip_delta = (max(clip) - min(clip)) if clip else None
        print(
            f"  {phase:10s} n={len(items):4d}"
            f"  applied P50/P90/max={_fmt(percentile(applied, 0.5))}/"
            f"{_fmt(percentile(applied, 0.9))}/{_fmt(max(applied) if applied else None)} dB"
            f"  ≥{threshold_db:g}dB 占比 {share:.0%}"
            f"  段数P50={_fmt(percentile(active, 0.5), 0)}"
            f"  masking_peak P50={_fmt(percentile(peak, 0.5))}"
            f"  clip增量={clip_delta if clip_delta is not None else '?'}"
            f"  余量中位={_fmt(percentile(headroom_db, 0.5))} dB",
            flush=True,
        )
    print(f"  丢帧跳变 {gaps} 次", flush=True)


#: 中文提示语。段名保持 ASCII（CSV 好 grep），提示人话可以另写。
CUE_TEXT = {
    "ready": "准备，请听提示",
    "quiet": "请保持安静",
    "keyboard": "请开始敲键盘",
    "pause": "请停一下",
    "speech": "请开始说话",
    "knock": "请轻敲桌面",
    "wind": "请对着设备持续吹气",
    # 掩蔽矩阵验收段（2026-10-30）：
    "song_only": "只放歌，不要制造其它声音",
    "song_noise": "继续放歌，同时制造环境噪声",
    "song_talk": "继续放歌，请正常说话",
    "done": "采集结束",
}


class Cue:
    """换段提示。`say` 比蜂鸣好：蜂鸣本身就是瞬态，会跟键盘撞特征。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._say = shutil.which("say") if mode == "say" else None
        self._voice = None
        if mode == "say" and not self._say:
            print("!! 找不到 say，提示音退化为不发声（phase 标签仍然按时间走）", flush=True)
            self.mode = "none"
        if self._say:
            try:
                voices = subprocess.run([self._say, "-v", "?"], capture_output=True,
                                        text=True, timeout=5).stdout
                if "Tingting" in voices:
                    self._voice = "Tingting"
            except (OSError, subprocess.TimeoutExpired):
                pass

    def fire(self, phase: str) -> None:
        if self.mode == "say":
            text = CUE_TEXT.get(phase, phase)
            command = [self._say]
            if self._voice:
                command += ["-v", self._voice]
            try:
                # 不等待：等它说完会拖慢换段时刻；说话本身靠 guard 窗剔除。
                subprocess.Popen(command + [text], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                pass
        elif self.mode == "beep":
            sys.stdout.write("\a")
            sys.stdout.flush()


def fetch_snapshot(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="从在位上位机采集 24 段环境谱")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out", required=True, help="CSV 输出路径（目录不存在会自动建）")
    parser.add_argument("--seconds", type=float, default=60.0, help="无 --script 时的总时长")
    parser.add_argument("--script", default="", help="分段：quiet:10,keyboard:15,wind:12")
    parser.add_argument("--cue", choices=("say", "beep", "none"), default="say")
    parser.add_argument("--poll-hz", type=float, default=30.0)
    parser.add_argument("--guard", type=float, default=2.0,
                        help="每段开头多少秒标 cue=1（操作提示音未散尽）")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    phases = parse_script(args.script)
    duration = total_seconds(phases, args.seconds)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cue = Cue(args.cue)
    period = 1.0 / max(args.poll_hz, 5.0)

    started = time.monotonic()
    last_seq: int | None = None
    rows: list[dict] = []
    # 累计边界 cum[k] = 前 k 段的秒数。段 i 覆盖 [cum[i], cum[i+1])。
    cum = [0.0]
    for _, length in phases:
        cum.append(cum[-1] + length)
    phase_index = 0 if phases else -1
    phase_started = started
    phase_name = phases[0][0] if phases else "collect"
    gaps = 0

    print(f"采集 {duration:.1f}s -> {out_path}", flush=True)
    if phases:
        print("顺序：" + " → ".join(f"{name} {length:g}s" for name, length in phases), flush=True)
        cue.fire(phases[0][0])

    try:
        while True:
            now = time.monotonic()
            elapsed = now - started
            if elapsed >= duration:
                break

            # 换段：按累计边界找当前段，不累计计数（旧写法错位一段，实测抓到过）。
            if phases:
                target = phase_index
                while target + 1 < len(phases) and elapsed >= cum[target + 1]:
                    target += 1
                if target != phase_index:
                    phase_index = target
                    phase_name, phase_len = phases[phase_index]
                    phase_started = now
                    cue.fire(phase_name)
                    print(f"  [{elapsed:6.1f}s] 进入 {phase_name}（{phase_len:g}s）", flush=True)

            try:
                snapshot = fetch_snapshot(args.url, args.timeout)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"!! 读不到上位机快照：{exc}", flush=True)
                return 1

            frame = snapshot.get("frame") or {}
            bands = frame.get("ambient_band_dbfs")
            if not bands:
                print("!! 帧里没有 ambient_band_dbfs；上位机连的可能是旧固件或回放源", flush=True)
                return 1
            seq = frame.get("sequence")
            in_phase = phase_name if phases else "collect"
            phase_elapsed = now - phase_started
            row = {
                "wall_iso": datetime.now().isoformat(timespec="milliseconds"),
                "elapsed_s": f"{elapsed:.3f}",
                "phase": in_phase,
                "cue": 1 if (phases and phase_elapsed < args.guard) else 0,
                "seq": seq,
                "device_ts_ms": frame.get("timestamp_ms"),
                "context": frame.get("context"),
                "context_score": frame.get("context_score"),
                "context_valid": frame.get("context_valid"),
                "event": frame.get("event"),
                "event_valid": frame.get("event_valid"),
                "environment": frame.get("environment"),
                "state": frame.get("state"),
                "mode": frame.get("mode"),
                "conversation": frame.get("conversation"),
                "speech_gain_db": frame.get("speech_gain_db"),
                "media_percent": frame.get("media_percent"),
                "own_voice_confidence": frame.get("own_voice_confidence"),
                "imu_motion": frame.get("imu_motion"),
                "ambient_open": frame.get("ambient_open"),
            }
            row.update(systemeq_columns(snapshot))
            for index in range(BAND_COUNT):
                row[f"band_{index:02d}"] = (
                    f"{float(bands[index]):.2f}" if index < len(bands) else ""
                )

            # 同 sequence 只落一次；seq 回绕/重连从大跳到小，也算新帧但不计 gap。
            is_new = seq is None or last_seq is None or seq != last_seq
            if is_new:
                if last_seq is not None and isinstance(seq, int) and isinstance(last_seq, int):
                    step = seq - last_seq
                    if step > 1:
                        gaps += 1
                rows.append(row)
                last_seq = seq

            time.sleep(period)
    except KeyboardInterrupt:
        print("\n手动停止", flush=True)

    elapsed = time.monotonic() - started
    if not rows:
        print("!! 一帧都没采到，不写文件", flush=True)
        return 1

    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    rate = len(rows) / elapsed if elapsed > 0 else 0.0
    contexts = sorted({str(row["context"]) for row in rows})
    print(
        f"完成：{len(rows)} 帧 / {elapsed:.1f}s（{rate:.2f} 帧/秒），"
        f"丢帧跳变 {gaps} 次，模型输出集合 {contexts}",
        flush=True,
    )
    print_summary(rows, gaps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
