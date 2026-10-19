"""同时采「16 kHz 音频 + 10 Hz 24 段频段」——教师-学生蒸馏的数据入口。

为什么必须同时采：蒸馏要的是"音频上的教师标签"对齐到"设备上的学生特征"。分两次录
（一次录音频、一次录频段）在时间上对不齐，而 1 秒窗的事件标签差 200 ms 就会教错。
设备在**无限录音**（`rec start` 不带秒数）时控制节拍仍是 100 ms，音频块与 `earbud_command`
都带设备 `timestamp_ms`，所以两者可以按设备时间精确对齐——这是这个工具存在的全部理由。

口径（写清楚，别以后拍脑袋）：

* 音频：16 kHz / 单声道 / 16-bit，每块通常 320 样本（20 ms），`mic_chunk.timestamp_ms`
  是该块开始时的设备毫秒。
* 频段：`earbud_command.timestamp_ms` 是该帧分析的设备毫秒；帧率应为 10 Hz（无限录音）。
  如果实测掉到 4 Hz，说明固件把有限时长录音的 250 ms 节拍用上了，采样脚本会报错。
* 落盘：`audio.wav`、`frames.jsonl`（每行一帧，带设备时间与频段）、`meta.json`（计数、
  帧率、音频长度、首尾时间、丢块）。音频块序号不连续时在 meta 里逐段记录，不静默补零。

用法（需要带 pyserial 的解释器；本机用 host_up.sh 里那个 uv python）：

    python3 tools/collect_teacher_data.py --port /dev/cu.usbmodem101 \
        --seconds 120 --out 记录/教师数据/office-01

    # 采完后离线检查对齐与帧率
    python3 tools/collect_teacher_data.py --check 记录/教师数据/office-01

**串口独占**：跑之前先停上位机（面板 → /api/stop，或 curl http://127.0.0.1:8787/api/stop）。
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

RATE = 16000

#: 分段提示语（说给操作者听；与采集本身无关）。段名保持 ASCII，方便离线按段切。
CUE_TEXT = {
    "ready": "准备",
    "quiet": "请保持安静",
    "keyboard": "请敲键盘",
    "wind": "请对着设备吹气",
    "speech": "请开始说话",
    "knock": "请轻敲桌面",
    "pause": "请停一下",
    "done": "采集结束",
}


def parse_script(text: str) -> list[tuple[str, float]]:
    phases = []
    for item in (text or "").split(","):
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


def run_cue_thread(phases: list[tuple[str, float]], started: float,
                   boundaries: list[dict]) -> None:
    """按累计时间换段：说提示语并记录边界（host 单调毫秒，供离线对齐设备时间）。"""
    voice = None
    say = shutil.which("say")
    if say:
        try:
            listing = subprocess.run([say, "-v", "?"], capture_output=True, text=True,
                                     timeout=5).stdout
            voice = "Tingting" if "Tingting" in listing else None
        except (OSError, subprocess.TimeoutExpired):
            voice = None
    accumulated = 0.0
    for name, length in phases:
        wait = accumulated - (time.monotonic() - started)
        if wait > 0:
            time.sleep(wait)
        boundaries.append({"phase": name,
                           "host_ms": int(round((time.monotonic() - started) * 1000))})
        if say:
            command = [say] + (["-v", voice] if voice else []) + [CUE_TEXT.get(name, name)]
            try:
                subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                pass
        accumulated += length


class MissingDependency(Exception):
    pass


def load_serial():
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - 仅在缺依赖时触发
        raise MissingDependency(
            "需要 pyserial。用本机带 pyserial 的解释器，例如\n"
            "  /Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/"
            "archive-v0/LSKE5uaT0ygsXUNE/bin/python"
        ) from exc
    return serial


class Capture:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.wav = wave.open(str(out_dir / "audio.wav"), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(RATE)
        self.frames_path = (out_dir / "frames.jsonl").open("w", encoding="utf-8")
        self.chunks = 0
        self.audio_samples = 0
        self.frame_count = 0
        self.bad_lines = 0
        self.chunk_gaps: list[tuple[int, int]] = []
        self.last_chunk_sequence: int | None = None
        self.first_frame_ts: int | None = None
        self.last_frame_ts: int | None = None
        self.first_chunk_ts: int | None = None
        self.last_chunk_ts: int | None = None
        self.event_counts: dict[str, int] = {}

    def feed(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith("{"):
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.bad_lines += 1
            return
        kind = payload.get("type")
        if kind == "mic_chunk":
            self._chunk(payload)
        elif kind == "earbud_command" and payload.get("ambient_band_dbfs"):
            self._frame(payload)

    def _chunk(self, payload: dict) -> None:
        pcm = payload.get("pcm")
        if not isinstance(pcm, str):
            return
        try:
            raw = base64.b64decode(pcm, validate=True)
        except (ValueError, base64.binascii.Error):
            self.bad_lines += 1
            return
        if len(raw) % 2 != 0:
            self.bad_lines += 1
            return
        sequence = payload.get("sequence")
        timestamp = payload.get("timestamp_ms")
        if isinstance(sequence, int):
            if self.last_chunk_sequence is not None and sequence != self.last_chunk_sequence + 1:
                self.chunk_gaps.append((self.last_chunk_sequence, sequence))
            self.last_chunk_sequence = sequence
        if isinstance(timestamp, int):
            if self.first_chunk_ts is None:
                self.first_chunk_ts = timestamp
            self.last_chunk_ts = timestamp
        self.wav.writeframes(raw)
        self.chunks += 1
        self.audio_samples += len(raw) // 2

    def _frame(self, payload: dict) -> None:
        timestamp = payload.get("timestamp_ms")
        if isinstance(timestamp, int):
            if self.first_frame_ts is None:
                self.first_frame_ts = timestamp
            self.last_frame_ts = timestamp
        record = {
            "host_time_ms": int(time.monotonic() * 1000),
            "t_ms": timestamp,
            "bands": payload["ambient_band_dbfs"],
            "environment": payload.get("environment"),
            "event": payload.get("event"),
            "event_valid": payload.get("event_valid"),
            "context": payload.get("context"),
            "context_valid": payload.get("context_valid"),
            "speech_gate": payload.get("speech_gate"),
            "speech_gate_valid": payload.get("speech_gate_valid"),
            "speech_gate_us": payload.get("speech_gate_us"),
            "media_percent": payload.get("media_percent"),
        }
        self.frames_path.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.frame_count += 1
        event = payload.get("event")
        if isinstance(event, str):
            self.event_counts[event] = self.event_counts.get(event, 0) + 1

    def close(self, seconds: float) -> dict:
        self.wav.close()
        self.frames_path.close()
        frame_rate = self.frame_count / seconds if seconds > 0 else 0.0
        audio_seconds = self.audio_samples / RATE
        meta = {
            "requested_seconds": seconds,
            "frames": self.frame_count,
            "frame_rate_hz": round(frame_rate, 3),
            "audio_seconds": round(audio_seconds, 3),
            "chunks": self.chunks,
            "chunk_gaps": self.chunk_gaps,
            "bad_lines": self.bad_lines,
            "first_frame_ts_ms": self.first_frame_ts,
            "last_frame_ts_ms": self.last_frame_ts,
            "first_chunk_ts_ms": self.first_chunk_ts,
            "last_chunk_ts_ms": self.last_chunk_ts,
            "event_counts": self.event_counts,
            "note": "audio.wav 与 frames.jsonl 按设备 timestamp_ms 对齐；frame_rate 应为 ~10 Hz",
        }
        (self.out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta


def capture(port: str, baud: int, seconds: float, out_dir: Path,
            phases: list[tuple[str, float]] | None = None, no_cue: bool = False) -> int:
    serial = load_serial()
    capture_state = Capture(out_dir)
    started = time.monotonic()
    boundaries: list[dict] = []
    if phases:
        seconds = sum(length for _, length in phases)
        if not no_cue:
            threading.Thread(target=run_cue_thread, args=(phases, started, boundaries),
                             daemon=True).start()
        else:
            accumulated = 0.0
            for name, length in phases:
                boundaries.append({"phase": name, "host_ms": int(round(accumulated * 1000))})
                accumulated += length
    try:
        with serial.Serial(port=port, baudrate=baud, timeout=0.2) as connection:
            connection.dtr = True
            connection.rts = True
            time.sleep(0.3)
            # 无限录音：固件只在**有限时长**录音时放宽到 250 ms，无限流保持 100 ms。
            connection.write(b"rec start\n")
            connection.flush()
            print(f"开始采集 {seconds:.0f} s -> {out_dir}", flush=True)
            while time.monotonic() - started < seconds:
                line = connection.readline()
                if line:
                    capture_state.feed(line)
            connection.write(b"rec stop\n")
            connection.flush()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n手动停止", flush=True)
    finally:
        elapsed = time.monotonic() - started
        meta = capture_state.close(elapsed)
        (out_dir / "phases.json").write_text(
            json.dumps({"phases": boundaries}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    print(
        f"完成：{meta['frames']} 帧（{meta['frame_rate_hz']:.2f} 帧/秒）、"
        f"{meta['audio_seconds']:.1f} s 音频、{meta['chunks']} 块、"
        f"丢块跳变 {len(meta['chunk_gaps'])} 次、坏行 {meta['bad_lines']}"
    )
    print(f"事件分布：{meta['event_counts']}")
    if meta["frames"] == 0:
        print("!! 没有帧；确认固件在无限录音模式下仍按 100 ms 发 earbud_command", file=sys.stderr)
        return 1
    if meta["frame_rate_hz"] < 8.0:
        print(
            f"!! 帧率 {meta['frame_rate_hz']:.2f} Hz 低于 8 Hz：说明录音时用了 250 ms 节拍，"
            "这批数据不能用于 10 Hz 学生特征",
            file=sys.stderr,
        )
        return 1
    if meta["audio_seconds"] < seconds * 0.8:
        print("!! 音频比请求时长短 20% 以上，检查 USB 丢块", file=sys.stderr)
        return 1
    return 0


def check(out_dir: Path) -> int:
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    frames = [json.loads(line) for line in
              (out_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line]
    print(f"帧 {len(frames)}，音频 {meta['audio_seconds']} s，帧率 {meta['frame_rate_hz']} Hz")
    if not frames:
        return 1
    t = [f["t_ms"] for f in frames if isinstance(f.get("t_ms"), int)]
    if len(t) > 1:
        steps = [b - a for a, b in zip(t, t[1:])]
        steps.sort()
        median = steps[len(steps) // 2]
        print(f"帧间隔中位 {median} ms（应约 100）；最大 {max(steps)} ms")
        if not 80 <= median <= 120:
            print("!! 帧间隔不在 100±20 ms，不能用 10 Hz 学生特征", file=sys.stderr)
            return 1
    first = min(x for x in [meta.get("first_frame_ts_ms"), meta.get("first_chunk_ts_ms")]
                if isinstance(x, int))
    last = max(x for x in [meta.get("last_frame_ts_ms"), meta.get("last_chunk_ts_ms")]
               if isinstance(x, int))
    print(f"设备时间覆盖 {last - first} ms；音频与帧的首个时间戳差 "
          f"{abs((meta.get('first_frame_ts_ms') or 0) - (meta.get('first_chunk_ts_ms') or 0))} ms")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同时采设备音频与 10 Hz 频段")
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--script", default="",
                        help="分段：ready:6,quiet:20,keyboard:40,wind:30,speech:30,knock:12")
    parser.add_argument("--no-cue", action="store_true", help="分段但不播报提示语")
    parser.add_argument("--check", type=Path, help="检查已有采集目录")
    args = parser.parse_args()
    if args.check:
        return check(args.check)
    if not args.port or not args.out:
        parser.error("采集模式需要 --port 与 --out")
    try:
        return capture(args.port, args.baud, args.seconds, args.out,
                       parse_script(args.script) or None, args.no_cue)
    except MissingDependency as exc:
        print(f"依赖未就位：\n{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
