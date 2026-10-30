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

import array
import base64
import json
import math
import subprocess
import sys
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
        learn: bool = False,
        on_verdict=None,
        activity_provider=None,
    ) -> None:
        self.python = str(python)
        self.worker = str(worker)
        self.enroll_dir = str(enroll_dir)
        self.threshold = float(threshold)
        self.enabled = enabled
        #: 在线原型积累（2026-10-28 事故后默认关）：worker 侧还有锚点上界兜底，
        #: 但默认不学才是安全状态；要自适应必须显式 --voiceprint-learn。
        self.learn = bool(learn)
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
        #: 在线积累（2026-10-23）：高置信 self 窗口更新原型后的累计次数 / 来源文件。
        self._learn_updates = 0
        self._learned_from = ""
        #: 2026-10-28：积累原型与注册锚点的 cosine（drift）。低于 worker 的
        #: anchor_min_cos 时 worker 会回退并冻结学习；这里只做展示与告警。
        self._drift: float | None = None
        self._learn_frozen = False
        self._learn_rejected = ""
        self._updated_at: float | None = None
        self._fed_chunks = 0
        self._dropped_chunks = 0
        self._sent_verdicts = 0
        self._last_sent_verdict: str | None = None
        self._history: deque[dict] = deque(maxlen=120)
        self._stderr_tail: list[str] = []
        #: 能量兜底：S3 的 voice_likelihood 对"近场但小声"会漏报（2026-10-22：用户
        #: 对着说话时设备端只有 0.2，声纹 worker 于是永远收到 speech_active=false，
        #: 哪怕 WavLM 对同一段音频的 self 相似度是 0.95）。这里用块 RMS 做第二证据。
        self._energy_history: deque[float] = deque(maxlen=200)
        self._activity_chunks = 0

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

    def _worker_argv(self) -> list[str]:
        """worker 启动参数。learn=False（默认）时必须显式传 --no-learn。

        2026-10-28 事故：host 原先不传任何学习开关，worker 就按默认开着 8% EMA，
        故这条开关必须可测，而不是埋在 start() 里。
        """
        argv = [
            self.python, self.worker,
            "--enroll-dir", self.enroll_dir,
            "--threshold", str(self.threshold),
        ]
        if not self.learn:
            argv.append("--no-learn")
        return argv

    def start(self) -> None:
        if not self._available:
            return
        argv = self._worker_argv()
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

    @staticmethod
    def _chunk_dbfs(chunk) -> float | None:
        """块 RMS（dBFS）。上位机不依赖 numpy，16 kHz 单声道 int16 直接算。"""
        raw = bytes(getattr(chunk, "pcm", b"") or b"")
        raw = raw[: len(raw) - len(raw) % 2]
        if len(raw) < 320:  # <20 ms 的碎片不参与判据
            return None
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(float(value) * value for value in samples) / len(samples))
        return 20.0 * math.log10(max(rms, 1.0) / 32768.0)

    def _energy_floor(self) -> float | None:
        values = self._energy_history
        if len(values) < 20:
            return None
        ordered = sorted(values)
        return ordered[int(len(ordered) * 0.2)]

    def feed(self, chunk) -> None:
        """chunk 是 host.protocol.MicChunk（有 .pcm 和 .rate）。"""
        if not self._ready or self._process is None or self._process.stdin is None:
            return
        try:
            payload = {
                "rate": int(getattr(chunk, "rate", 16000)),
                "pcm_base64": base64.b64encode(bytes(chunk.pcm)).decode("ascii"),
            }
            speech_active = False
            if self.activity_provider is not None:
                try:
                    frame = self.activity_provider()
                    speech_active = bool(
                        frame is not None
                        and getattr(frame, "event", None) == "speech"
                        and float(getattr(frame, "voice_likelihood", 0.0)) >= 0.6
                    )
                except Exception:  # noqa: BLE001 - activity missing must fail closed
                    speech_active = False
            # 第二证据：块能量显著高于近 20 s 本底（+6 dB）也算说话。
            # S3 的 voice_likelihood 在"用户小声/设备在侧"时会漏；声纹判定本身
            # 仍由 WavLM 的 cosine 决定，能量只决定"这个窗值不值得判"。
            dbfs = self._chunk_dbfs(chunk)
            if dbfs is not None:
                with self._lock:
                    self._energy_history.append(dbfs)
                    floor = self._energy_floor()
                if floor is not None and dbfs >= floor + 6.0 and dbfs > -48.0:
                    speech_active = True
                if speech_active:
                    with self._lock:
                        self._activity_chunks += 1
            payload["speech_active"] = speech_active
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
                self._learn_updates = int(message.get("learn_updates", 0))
                self._learned_from = str(message.get("learned_from") or "")
                drift = message.get("drift")
                if drift is not None:
                    self._drift = float(drift)
                self._learn_frozen = bool(message.get("learn_frozen", False))
                self._error = ""
            elif kind == "learn_reject":
                # worker 发现已积累原型离注册锚点太远（2026-10-28 事故）：
                # 拒绝加载、留档旧文件、冻结学习。必须让面板一眼看见。
                drift = message.get("drift")
                self._drift = float(drift) if drift is not None else None
                self._learn_frozen = True
                self._learn_rejected = str(message.get("rejected_to") or "")
            elif kind == "learn_frozen":
                drift = message.get("drift")
                if drift is not None:
                    self._drift = float(drift)
                self._learn_frozen = True
            elif kind == "verdict":
                self._verdict = str(message.get("verdict"))
                self._score = message.get("score")
                self._smoothed = message.get("smoothed")
                self._dbfs = message.get("dbfs")
                self._speech_ratio = message.get("speech_ratio")
                self._windows = int(message.get("windows", self._windows))
                self._learn_updates = int(
                    message.get("learn_updates", self._learn_updates))
                drift = message.get("drift")
                if drift is not None:
                    self._drift = float(drift)
                if "learn_frozen" in message:
                    self._learn_frozen = bool(message.get("learn_frozen"))
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
                "learn_updates": self._learn_updates,
                "learned_from": self._learned_from,
                # 2026-10-28：在线积累的安全性遥测。drift = 积累原型与注册锚点的
                # cosine，正常应接近 1.0；低于 worker 的 anchor_min_cos 就冻结。
                "learn_enabled": self.learn,
                "learn_frozen": self._learn_frozen,
                "learn_rejected": self._learn_rejected,
                "drift": self._drift,
                "latency_ms": self._latency_ms,
                "fed_chunks": self._fed_chunks,
                "dropped_windows": self._dropped_chunks,
                "sent_verdicts": self._sent_verdicts,
                "last_sent_verdict": self._last_sent_verdict,
                "energy_floor_dbfs": (
                    None if self._energy_floor() is None
                    else round(self._energy_floor(), 1)
                ),
                "activity_chunks": self._activity_chunks,
                "history": list(self._history),
                "age_s": age,
                "stderr_tail": list(self._stderr_tail),
                "mode": "self-veto",
            }
