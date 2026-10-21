"""训练数据落盘：每个候选片段一条 JSONL + 可选 WAV。

用途（D34 / C 路线）：把线上跑出来的真实场景收成"端侧蒸馏"数据。记录里同时保存：

* 端侧看到的输入：16 kHz PCM（可选）、10 Hz 遥测（conversation / voice_likelihood /
  event / ambient_open 意图）、声纹轮次；
* 教师标签：ASR 原始/清洗后轮次、LLM 的结构化判定与原始响应、上下文；
* 执行侧结果：快路径环境声实际开关、LLM 迟到否决的时延；
* 人工标签占位（`labels.human=null`），后续可用 `tools/label_training_data.py` 补。

落盘位置默认由 `host_up.sh` 指到 `记录/训练数据/<会话时间>/`，整个 `记录/` 在
`.gitignore` 里；音频默认保存，可用 `--no-training-audio` 关掉。数据格式变化时
`SCHEMA` 加一，旧记录仍可读。
"""

from __future__ import annotations

import json
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

#: 记录 schema 版本。只增键/加字段时不必涨版本；改语义/删键才涨。
SCHEMA = 1

#: 单个会话默认最多写多少字节（JSONL + WAV），防止演示机磁盘被长跑撑爆。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024


class TrainingWriter:
    def __init__(
        self,
        directory: str | Path,
        *,
        audio: bool = True,
        max_bytes: int | None = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = Path(directory)
        self.audio_enabled = bool(audio)
        self.max_bytes = int(max_bytes or 0)
        self.started_at = time.monotonic()
        self.started_wall = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.session_dir = self.root / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.segments_path = self.session_dir / "segments.jsonl"
        self.events_path = self.session_dir / "events.jsonl"
        self.audio_dir = self.session_dir / "audio"
        if self.audio_enabled:
            self.audio_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._bytes = 0
        self._segments = 0
        self._events = 0
        self._closed = False
        self._write_session_meta()

    def _write_session_meta(self) -> None:
        meta = {
            "schema": SCHEMA,
            "started_wall": self.started_wall,
            "audio_enabled": self.audio_enabled,
            "note": "teacher labels come from ASR+LLM; labels.human is null until labeled",
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @property
    def session_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 1)

    @property
    def segments_written(self) -> int:
        with self._lock:
            return self._segments

    def _budget_left(self) -> bool:
        return not self.max_bytes or self._bytes < self.max_bytes

    def _append(self, path: Path, record: dict) -> bool:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            if self._closed or not self._budget_left():
                return False
            with path.open("a", encoding="utf-8", buffering=1) as handle:
                handle.write(line)
            self._bytes += len(line.encode("utf-8"))
            return True

    def write_audio(self, name: str, pcm: bytes, rate: int) -> str | None:
        """把原始 16-bit mono PCM 写成 wav，返回相对会话目录的路径。"""
        if not self.audio_enabled or not pcm:
            return None
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)[:60]
        path = self.audio_dir / f"{safe}.wav"
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(max(int(rate), 1))
                wav.writeframes(pcm)
        except OSError:
            return None
        with self._lock:
            try:
                self._bytes += path.stat().st_size
            except OSError:
                pass
        return str(path.relative_to(self.session_dir))

    def write_segment(self, record: dict) -> dict:
        """追加一条片段记录。返回实际写入的记录（含 session/relative 字段）。"""
        record = dict(record)
        record.setdefault("schema", SCHEMA)
        record.setdefault("t_session_s", self.session_seconds)
        record["session"] = self.session_dir.name
        with self._lock:
            if self._closed or not self._budget_left():
                return {}
            self._segments += 1
        if not self._append(self.segments_path, record):
            with self._lock:
                self._segments -= 1
            return {}
        return record

    def write_event(self, kind: str, payload: dict) -> bool:
        record = {
            "schema": SCHEMA,
            "type": kind,
            "t_session_s": self.session_seconds,
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            **payload,
        }
        if self._append(self.events_path, record):
            with self._lock:
                self._events += 1
            return True
        return False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            segments, events, written = self._segments, self._events, self._bytes
        try:
            (self.session_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "segments": segments,
                        "events": events,
                        "bytes": written,
                        "duration_s": self.session_seconds,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
