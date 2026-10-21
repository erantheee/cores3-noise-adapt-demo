"""Mac 侧 WavLM self/other 声纹门控（环境声严格门控）。

WavLM-SV 模型约 360 MB，只能跑在 Mac/PC 上；上位机本身不依赖 torch/transformers，
所以这里把重依赖放进子进程 `tools/voiceprint_worker.py`（用仓库 `.venv/bin/python`），
上位机只负责把 S3 中继的 16 kHz PCM 喂过去、读回判定。

语义：`self` → 不开环境声；只有新鲜 `other` 才允许执行端按设备意图开放。
unknown / pending / stale / worker failure 一律关闭。
判定 3 秒没有新值由固件侧自动视为过期；串口关闭/进程退出时上位机发 `voiceprint off`。
默认关闭，只有显式 `--voiceprint` 才启用。
"""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class VoiceprintError(RuntimeError):
    """声纹 worker 无法启动或异常退出。"""


class VoiceprintService:
    """驱动 voiceprint_worker.py 的轻量客户端（鸭子类型与 relay/ambient 一致）。"""

    def __init__(
        self,
        python: str | Path = ROOT / ".venv" / "bin" / "python",
        worker: str | Path = ROOT / "tools" / "voiceprint_worker.py",
        enroll_dir: str | Path = ROOT / "记录" / "声纹" / "self-device",
        threshold: float = 0.80,
        enabled: bool = True,
        on_verdict=None,
        activity_provider=None,
    ) -> None:
        self.python = str(python)
        self.worker = str(worker)
        self.enroll_dir = str(enroll_dir)
        self.threshold = float(threshold)
        self.enabled = enabled
        #: 判定回调：host 用它把 self/other 回传 S3。
        self.on_verdict = on_verdict
        self.activity_provider = activity_provider
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._ready = False
        self._available = enabled and Path(self.python).exists() and Path(self.worker).exists()
        self._error = "" if self._available else "缺少 .venv/bin/python 或 voiceprint_worker.py"
        self._loading = False
        self._verdict: str | None = None
        self._score: float | None = None
        self._smoothed: float | None = None
        self._dbfs: float | None = None
        self._speech_ratio: float | None = None
        self._windows = 0
        self._latency_ms: float | None = None
        self._clips = 0
        self._dim = 0
        self._updated_at: float | None = None
        self._fed_chunks = 0
        self._dropped_chunks = 0
        self._sent_verdicts = 0
        self._last_sent_verdict: str | None = None
        self._history: deque[dict] = deque(maxlen=120)
        self._stderr_tail: list[str] = []

    # ------------------------------------------------------------------ 生命周期

    @property
    def ready(self) -> bool:
        """worker 是否已经报过 ready（模型 + 注册声纹都加载完）。

        `on_open` 只有在 ready 时才发 `voiceprint pending`：worker 起不来时发出去，
        设备会进入"等声纹判定"的状态而永远等不到后续判定。
        """
        with self._lock:
            return self._ready

    @property
    def available(self) -> bool:
        """解释器/worker 文件是否存在（静态条件，不表示模型已加载）。"""
        with self._lock:
            return self._available

    def start(self) -> None:
        if not self._available:
            return
        argv = [
            self.python, self.worker,
            "--enroll-dir", self.enroll_dir,
            "--threshold", str(self.threshold),
        ]
        try:
            self._process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except OSError as exc:
            with self._lock:
                self._available = False
                self._error = f"声纹 worker 启动失败：{exc}"
            return
        with self._lock:
            self._loading = True
        self._threads = [
            threading.Thread(target=self._read_stdout, name="voiceprint-out", daemon=True),
            threading.Thread(target=self._read_stderr, name="voiceprint-err", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self._process = None

    # ------------------------------------------------------------------ 数据

    def feed(self, chunk) -> None:
        """chunk 是 host.protocol.MicChunk（有 .pcm 和 .rate）。"""
        if not self._ready or self._process is None or self._process.stdin is None:
            return
        try:
            payload = {
                "rate": int(getattr(chunk, "rate", 16000)),
                "pcm_base64": base64.b64encode(bytes(chunk.pcm)).decode("ascii"),
            }
            if self.activity_provider is not None:
                try:
                    frame = self.activity_provider()
                    payload["speech_active"] = bool(
                        frame is not None
                        and getattr(frame, "event", None) == "speech"
                        and float(getattr(frame, "voice_likelihood", 0.0)) >= 0.6
                    )
                except Exception:  # noqa: BLE001 - activity missing must fail closed
                    payload["speech_active"] = False
            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()
            with self._lock:
                self._fed_chunks += 1
        except (OSError, ValueError) as exc:
            with self._lock:
                self._error = f"写入声纹 worker 失败：{exc}"
                self._ready = False

    # ------------------------------------------------------------------ 线程

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._apply(message)
        with self._lock:
            if not self._stop.is_set():
                self._ready = False
                self._error = self._error or "声纹 worker 已退出"

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            text = line.rstrip()
            if text:
                with self._lock:
                    self._stderr_tail = (self._stderr_tail + [text])[-20:]

    def note_sent(self) -> None:
        with self._lock:
            self._sent_verdicts += 1

    def _apply(self, message: dict) -> None:
        kind = message.get("type")
        now = time.monotonic()
        callback = None
        with self._lock:
            self._updated_at = now
            if kind == "loading":
                self._loading = True
            elif kind == "ready":
                self._loading = False
                self._ready = True
                self._clips = int(message.get("clips", 0))
                self._dim = int(message.get("dim", 0))
                self._error = ""
            elif kind == "verdict":
                self._verdict = str(message.get("verdict"))
                self._score = message.get("score")
                self._smoothed = message.get("smoothed")
                self._dbfs = message.get("dbfs")
                self._speech_ratio = message.get("speech_ratio")
                self._windows = int(message.get("windows", self._windows))
                self._latency_ms = message.get("latency_ms")
                self._last_sent_verdict = self._verdict
                start_sample = message.get("start_sample")
                end_sample = message.get("end_sample")
                if isinstance(start_sample, int) and isinstance(end_sample, int):
                    self._history.append({
                        "verdict": self._verdict,
                        "score": self._score,
                        "smoothed": self._smoothed,
                        "dbfs": self._dbfs,
                        "speech_ratio": self._speech_ratio,
                        "start_sample": start_sample,
                        "end_sample": end_sample,
                    })
                callback = self.on_verdict
            elif kind == "dropped":
                self._dropped_chunks += 1
            elif kind == "error":
                self._error = str(message.get("error"))
                self._ready = False

        if callback is not None:
            try:
                callback(self._verdict, float(self._score or 0.0))
            except Exception:  # noqa: BLE001 - 回调失败不能拖垮 worker 读取线程
                pass

    # ------------------------------------------------------------------ 读出

    def snapshot(self) -> dict:
        with self._lock:
            age = None if self._updated_at is None else round(time.monotonic() - self._updated_at, 1)
            return {
                "enabled": self.enabled,
                "available": self._available,
                "ready": self._ready,
                "loading": self._loading,
                "error": self._error,
                "verdict": self._verdict,
                "score": self._score,
                "smoothed": self._smoothed,
                "dbfs": self._dbfs,
                "speech_ratio": self._speech_ratio,
                "threshold": self.threshold,
                "windows": self._windows,
                "clips": self._clips,
                "dim": self._dim,
                "latency_ms": self._latency_ms,
                "fed_chunks": self._fed_chunks,
                "dropped_windows": self._dropped_chunks,
                "sent_verdicts": self._sent_verdicts,
                "last_sent_verdict": self._last_sent_verdict,
                "history": list(self._history),
                "age_s": age,
                "stderr_tail": list(self._stderr_tail),
                "mode": "self-veto",
            }
