"""连续音频落盘：把中继进来的 16 kHz PCM 整段写成 WAV（供 TSE / 听感 / 复盘）。

和 `training.py` 的 6 s 分段是两件事，不能互相替代：

* `TrainingWriter` 存的是 LLM 判定窗口（每段 6 s，段间还有分析节拍造成的间隔），拼起来
  有**人工断裂**，不能用来做目标说话人提取（TSE）、双声/梳状滤波判断或 A/B/C 听感对照；
* 这里按到达顺序把每一块 PCM 连续写进同一个 WAV，轮转处保证样本连续，丢块显式记账，
  不补零、不重采样。TSE 要的是"真实连续对话"：目标连续说话、插话、重叠、停顿都不能被
  切掉。

目录结构（每个会话一个目录，整个 `记录/` 在 .gitignore 里）：

    记录/连续音频/<YYYYMMDD-HHMMSS>/
        session.json     采样率、开始时间、轮转长度、磁盘预算、来源
        capture.jsonl    每块一行：序号 / 设备时间戳 / 样本数 / 文件与偏移 / 是否丢块
        audio-0001.wav   16 kHz 单声道 16-bit，按 chunk_seconds 轮转
        summary.json     关闭时写：时长 / 块数 / 丢块 / 文件 / 结束原因

纪律（与项目其它采集工具一致）：

* 采样率不一致 → **拒绝写入并记原因**，绝不静默重采样（那会把"设备接错"伪装成正常数据）；
* 丢块 → 只记账，**不补零**（补零等于伪造一段安静，会污染声纹与 TSE 标签）；
* 磁盘预算到顶 → 停止写入并标记 `stopped_by_budget`，不悄悄占满演示机磁盘；
* `close()` 幂等：`/api/stop` 与正常退出两条路径会各调一次。

典型用法（`tools/host_up.sh` 里 `CORES3_CONTINUOUS_AUDIO=1` 即打开）：

    python3 -m host --continuous-audio 记录/连续音频 --relay-audio

只开连续录音、不开中继也可以：设备照常 `rec start`，音频只落盘不播放。
"""

from __future__ import annotations

import json
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from .protocol import MicChunk

#: 记录 schema 版本。只增键/加字段时不必涨版本；改语义/删键才涨。
SCHEMA = 1

#: 设备中继的固定采样率（main/mic_decimator.hpp: kOutputRate）。
DEFAULT_RATE = 16_000

#: 单个 WAV 的轮转长度。600 s @ 16 kHz = 19.2 MB；太短会碎，太长不便定位。
DEFAULT_CHUNK_SECONDS = 600.0

#: 一次会话默认最多写多少字节原始 PCM，防止演示机磁盘被长跑撑爆（默认 2 GB）。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024


class ContinuousAudioWriter:
    """把连续的 MicChunk 写成会话级 WAV + 时间轴 JSONL。

    线程模型：`feed()` 由串口读取线程调用，`stats()`/`close()` 可能由 HTTP 收尾线程调用，
    所以全部状态在 `self._lock` 里；`close()` 幂等。
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        rate: int = DEFAULT_RATE,
        chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
        max_bytes: int | None = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = Path(directory)
        self.rate = int(rate)
        self.chunk_samples = int(chunk_seconds * self.rate)
        self.max_bytes = int(max_bytes or 0)
        if self.rate <= 0:
            raise ValueError("采样率必须为正")
        if self.chunk_samples <= 0:
            raise ValueError("轮转长度必须为正")
        self._lock = threading.RLock()

        self.started_at = time.monotonic()
        self.started_wall = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        session_dir = self.root / stamp
        suffix = 1
        while session_dir.exists():
            suffix += 1
            session_dir = self.root / f"{stamp}-{suffix}"
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True)

        self._closed = False
        self._finalized = False
        self._error = ""
        self._stopped_by_budget = False
        self._chunks = 0
        self._gaps = 0
        self._lost_chunks = 0
        self._bytes = 0
        self._samples = 0
        self._expected_sequence: int | None = None
        self._segment = 0
        self._file: wave.Wave_write | None = None
        self._file_name = ""
        self._file_samples = 0
        self._files: list[str] = []
        self._capture_path = self.session_dir / "capture.jsonl"
        self._capture_log = self._capture_path.open("a", encoding="utf-8", buffering=1)
        self._write_session_meta()
        self._open_segment()

    # ---- 只读状态 -------------------------------------------------------

    @property
    def seconds(self) -> float:
        with self._lock:
            return round(self._samples / self.rate, 3)

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_dir": str(self.session_dir),
                "duration_s": round(self._samples / self.rate, 3),
                "chunks": self._chunks,
                "gaps": self._gaps,
                "lost_chunks": self._lost_chunks,
                "bytes": self._bytes,
                "files": list(self._files),
                "stopped_by_budget": self._stopped_by_budget,
                "error": self._error,
            }

    # ---- 写入 -----------------------------------------------------------

    def _write_session_meta(self) -> None:
        meta = {
            "schema": SCHEMA,
            "started_wall": self.started_wall,
            "rate": self.rate,
            "chunk_seconds": round(self.chunk_samples / self.rate, 3),
            "max_bytes": self.max_bytes,
            "source": "CoreS3 中继（main/mic_decimator.hpp: 48 kHz 抽取到 16 kHz）",
            "note": "连续音频用于 TSE / 听感 / 复盘；不是 6 s 训练数据分段",
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _open_segment(self) -> None:
        self._segment += 1
        self._file_name = f"audio-{self._segment:04d}.wav"
        handle = self.session_dir / self._file_name
        wav = wave.open(str(handle), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.rate)
        self._file = wav
        self._file_samples = 0
        self._files.append(self._file_name)

    def _close_segment(self) -> None:
        if self._file is None:
            return
        self._file.close()
        self._file = None

    def feed(self, chunk: MicChunk) -> bool:
        """写入一块 PCM。返回是否真的写进去了；失败原因见 `error`。"""
        pcm = bytes(chunk.pcm or b"")
        with self._lock:
            if self._closed or self._error:
                return False
            if int(chunk.rate) != self.rate:
                self._error = (
                    f"采样率不一致：设备报 {chunk.rate} Hz，连续音频配置 {self.rate} Hz；"
                    "拒绝写入（不静默重采样）"
                )
                return False
            if not pcm or len(pcm) % 2:
                self._error = f"PCM 长度异常（{len(pcm)} 字节，应为非零偶数）"
                return False
            if self._file is None:
                self._error = "内部错误：当前没有打开的 WAV 分段"
                return False

            gap = False
            sequence = chunk.sequence
            if isinstance(sequence, int):
                if self._expected_sequence is not None and sequence != self._expected_sequence:
                    gap = True
                    self._gaps += 1
                    if sequence > self._expected_sequence:
                        self._lost_chunks += sequence - self._expected_sequence
                    # sequence 回退 = 设备重启了采集：记 gap，但不把回退算成丢块。
                self._expected_sequence = sequence + 1

            # 轮转发生在**写当前块之前**：这样不会在会话末尾留下一个 0 字节的空 WAV，
            # 也不会把一块样本拆到两个文件里。
            if self._file_samples >= self.chunk_samples:
                self._close_segment()
                self._open_segment()
            offset_s = round(self._file_samples / self.rate, 3)
            self._file.writeframes(pcm)
            self._file_samples += len(pcm) // 2
            self._samples += len(pcm) // 2
            self._chunks += 1
            self._bytes += len(pcm)
            self._capture_log.write(
                json.dumps(
                    {
                        "t_host_s": round(time.monotonic() - self.started_at, 3),
                        "t_device_ms": chunk.timestamp_ms,
                        "seq": sequence,
                        "samples": len(pcm) // 2,
                        "rate": self.rate,
                        "file": self._file_name,
                        "offset_s": offset_s,
                        "gap": True if gap else None,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

            if self.max_bytes and self._bytes >= self.max_bytes:
                self._stopped_by_budget = True
                self._finalize("stopped_by_budget")
            return True

    # ---- 收尾 -----------------------------------------------------------

    def _write_summary(self, reason: str) -> None:
        summary = {
            "schema": SCHEMA,
            "session": self.session_dir.name,
            "ended_wall": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": round(self._samples / self.rate, 3),
            "chunks": self._chunks,
            "gaps": self._gaps,
            "lost_chunks": self._lost_chunks,
            "bytes": self._bytes,
            "files": list(self._files),
            "stopped_by_budget": self._stopped_by_budget,
            "error": self._error,
            "end_reason": reason,
        }
        try:
            (self.session_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    def _finalize(self, reason: str) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            self._closed = True
            try:
                self._close_segment()
            except OSError:
                pass
            try:
                self._capture_log.close()
            except OSError:
                pass
            self._write_summary(reason)
            details = f"{self.session_dir}/：{self.seconds:g} s，{self._chunks} 块"
            if self._gaps:
                details += f"，丢块 {self._gaps} 处 / {self._lost_chunks} 块"
            if self._error:
                details += f"，错误：{self._error}"
            print(f"连续音频  {details}")

    def close(self) -> None:
        """幂等收尾：写 summary.json、修 WAV 头。"""
        self._finalize("stopped")
