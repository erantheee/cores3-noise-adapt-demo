"""把 CoreS3 麦克风的 PCM 低延迟地送到 macOS 输出（中继）。

**这里最重要的一件事是：延迟必须可测。** 这条通路上真正的延迟由最慢的那个缓冲区决定，
而不是由我们自己那个队列决定。旧实现用 ffplay 当播放端，它既不提供反压、也不暴露播放位置，
上位机只能拿"我们写进去多少字节"当作播放进度——于是任何一次卡顿造成的积压都变成**永久且
不可见**的延迟，没有任何东西能把它拉回来。实测（tools/probe_player_buffer.py）按实时速率喂它
5 秒音频，0.94 秒就被全部吞下（吸收比 5.3×），这正说明"改我们的队列参数，延迟只动一点点"
是必然的。

现在的默认播放端是 tools/relay/relaysink：

  * 它把"交给播放器的帧数"与"播放器回报已播完的帧数"都对出来，两者之差就是**真实排队深度**；
  * 深度超过 --target-ms 就丢掉来不及时播的那一块（宁可断一下，也不要越听越晚）；
  * 它把输出**钉在指定设备**上（不改系统默认输出），设备不在时如实报出来；
  * 每 0.5 秒一行 JSON 走 stdout，由这里收下来交给面板。

ffplay 那条路保留着（`player=PLAYER_FFPLAY`），用途只有一个：A/B 对比与回归。
不要把它当作等价实现——它的延迟不可观测。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

#: 播放端的选择。用字符串而不是枚举：它要出现在命令行与日志里，人得看得懂。
PLAYER_SINK = "sink"
PLAYER_FFPLAY = "ffplay"

#: 允许播放端最多囤多久的音频。这是延迟**上限**，不是优化目标：实时流的生产速率与播放
#: 速率相同，任何一次抖动造成的积压都不会自己退回去，只能靠丢掉过期的那一块压住。
#: 60 ms 的取舍：实测稳态排队深度是 20–40 ms（一到两个 20 ms 包），60 ms 既不误丢，
#: 也不把下限抬高。真正不可压缩的是驱动侧那一段（AirPods 自报 170 ms，见 driver_ms）。
DEFAULT_TARGET_MS = 60.0

#: 麦克风电平刻意保守（板上约 −40 dB RMS），这里做补偿增益，再用 0.95 的限幅上限防削顶。
#: 这两个值与旧 ffplay 链的 `-af volume=18dB,alimiter=limit=0.95` 一致：换播放端不应该
#: 顺带改变音量，否则"延迟变了没有"就没法和以前比。
DEFAULT_GAIN_DB = 18.0
DEFAULT_LIMIT = 0.95

#: 中继期望声音最终到达的设备。这是**钉住引擎输出**用的（不改系统默认输出）：
#: 默认输出是用户的设置，擅自改会让"没声音"变成更难查的问题。
DEFAULT_DEVICE = "AirPods Pro"

#: 只对 ffplay 生效：允许它最多比"现在"多囤多久。旧实现唯一的延迟上限。
DEFAULT_BUDGET_SECONDS = 0.10

#: 超过这么久没有收到播放端的统计行，就认为它的数字已经不可信。面板必须能看出这一点——
#: "没有数字"和"数字是 0"是两件事。
SINK_STATS_STALE_S = 3.0


class RelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayStats:
    chunks_written: int
    chunks_dropped: int
    bytes_written: int
    #: 因为超出延迟预算而被丢掉的旧音频。只对 ffplay 生效；sink 模式下丢包发生在播放端
    #: 内部，由 live() 里的 dropped_ms 报出来。
    chunks_stale: int


class AudioRelay:
    """Serial parsing never blocks on playback.

    上位机侧的队列只做交接（默认 2 块 = 40 ms）。积压由播放端负责压住并报告，
    因为我们这边看不到播放器内部到底囤了多少。
    """

    def __init__(
        self,
        rate: int = 16000,
        queue_chunks: int = 2,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        player_command: list[str] | None = None,
        player: str = PLAYER_SINK,
        device: str = DEFAULT_DEVICE,
        target_ms: float = DEFAULT_TARGET_MS,
        gain_db: float = DEFAULT_GAIN_DB,
        limit: float = DEFAULT_LIMIT,
        sink_binary: str | None = None,
    ) -> None:
        self.rate = rate
        self.target_ms = target_ms
        self.gain_db = gain_db
        self.limit = limit
        self.device = device
        self.budget_bytes = int(rate * 2 * budget_seconds)
        self._requested_player = player
        #: 留出注入点：测试里用 `cat` 当"照单全收"的播放端，就不需要声卡也能验证预算。
        self._player_command = player_command
        self._sink_binary = sink_binary
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_chunks)
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stats_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._chunks_written = 0
        self._chunks_dropped = 0
        self._bytes_written = 0
        self._chunks_stale = 0
        self._committed_bytes = 0
        self._play_clock: float | None = None
        self._failure: str | None = None
        #: 播放端自报的最近一份状态（sink 才有）。None = 这个播放端不报告任何数字。
        self._live: dict | None = None
        self._live_at: float | None = None
        self._stderr_tail: list[str] = []

    # ------------------------------------------------------------------ 播放端

    @property
    def mode(self) -> str:
        if self._player_command is not None:
            return "custom"
        return self._requested_player

    def _sink_path(self) -> str:
        if self._sink_binary:
            return self._sink_binary
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "relay", "relaysink")

    def build_command(self) -> list[str]:
        """构造播放端命令行。sink 与 ffplay 的参数差异全部收在这里。"""
        if self._player_command is not None:
            return self._player_command
        if self._requested_player == PLAYER_SINK:
            binary = self._sink_path()
            if not os.path.exists(binary):
                raise RelayError(
                    f"中继播放端未构建（缺 {binary}）：先跑 sh tools/relay/build.sh"
                )
            command = [
                binary,
                "--rate", str(self.rate),
                "--target-ms", f"{self.target_ms:g}",
                "--gain-db", f"{self.gain_db:g}",
                "--limit", f"{self.limit:g}",
            ]
            if self.device:
                command += ["--device", self.device]
            return command
        ffplay = shutil.which("ffplay")
        if ffplay is None:
            raise RelayError("找不到 ffplay；请先安装 ffmpeg（brew install ffmpeg）")
        return [
            ffplay, "-nodisp", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay", "-avioflags", "direct",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "s16le", "-ar", str(self.rate), "-ch_layout", "mono",
            # CoreS3 的麦克风电平刻意保守，实测板上约 −40 dB RMS。先把它抬到能听清，
            # 再用限幅器压住峰值，而不是要求用户把 AirPods 音量拉满。
            "-af", f"volume={self.gain_db:g}dB,alimiter=limit={self.limit:g}",
            "-i", "pipe:0",
        ]

    def start(self) -> None:
        command = self.build_command()
        # sink 的 stdout 是**协议通道**（每行一个 JSON），必须接走；ffplay 的 stdout 没用。
        capture_stdout = self.mode == PLAYER_SINK
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stdout else None,
        )
        self._thread = threading.Thread(target=self._run, name="audio-relay", daemon=True)
        self._thread.start()
        if capture_stdout:
            self._stats_thread = threading.Thread(
                target=self._read_sink_stats, name="audio-relay-stats", daemon=True
            )
            self._stats_thread.start()
            self._stderr_thread = threading.Thread(
                target=self._read_sink_stderr, name="audio-relay-stderr", daemon=True
            )
            self._stderr_thread.start()

    def _read_sink_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for raw in self._process.stderr:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            with self._lock:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-10]

    def _read_sink_stats(self) -> None:
        """收播放端的状态行。解析不了的行不丢：进 stderr 尾巴，现场要看得见。

        播放端中途死掉时这里再也读不到东西——那种情况由 `failure` 与 `age_s` 表达，
        不能靠"最后一份数字"继续假装一切正常。
        """
        assert self._process is not None and self._process.stdout is not None
        for raw in self._process.stdout:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                with self._lock:
                    self._stderr_tail.append(f"播放端输出了非 JSON 行：{text[:120]}")
                    del self._stderr_tail[:-10]
                continue
            if not isinstance(event, dict):
                continue
            with self._lock:
                self._live = event
                self._live_at = time.monotonic()
                if event.get("state") == "error":
                    self._failure = str(event.get("message") or "播放端报错")

    # ------------------------------------------------------------------ 喂数据

    def feed(self, pcm: bytes, rate: int) -> None:
        if rate != self.rate:
            self._failure = f"设备采样率 {rate} Hz，与中继 {self.rate} Hz 不一致"
            return
        if self._failure is not None:
            return
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._chunks_dropped += 1
            try:
                self._queue.put_nowait(pcm)
            except queue.Full:
                with self._lock:
                    self._chunks_dropped += 1

    def _run(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        # 只有 ffplay 那条路需要我们当节流阀：它不给反压。sink 自己按真实播放进度决定丢不丢，
        # 我们再叠一层只会变成双重丢音。
        # 除了 sink，其它播放端（含测试里的自定义命令）都需要我们当节流阀。
        enforce_budget = self.mode != PLAYER_SINK
        try:
            while True:
                pcm = self._queue.get()
                if pcm is None:
                    break
                if enforce_budget:
                    now = time.monotonic()
                    if self._play_clock is None:
                        self._play_clock = now
                    ahead = self._committed_bytes - int(
                        (now - self._play_clock) * self.rate * 2
                    )
                    if ahead >= self.budget_bytes:
                        with self._lock:
                            self._chunks_stale += 1
                        continue
                self._process.stdin.write(pcm)
                self._process.stdin.flush()
                with self._lock:
                    self._chunks_written += 1
                    self._bytes_written += len(pcm)
                    self._committed_bytes += len(pcm)
        except (BrokenPipeError, OSError) as exc:
            self._failure = f"音频播放进程中止：{exc}"

    # ------------------------------------------------------------------ 收尾

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._process is not None:
            if self._process.stdin is not None:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass
            if self._process.poll() is None:
                self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()

    # ------------------------------------------------------------------ 读数

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def stats(self) -> RelayStats:
        with self._lock:
            return RelayStats(
                self._chunks_written,
                self._chunks_dropped,
                self._bytes_written,
                self._chunks_stale,
            )

    def live(self) -> dict | None:
        """给面板看的实时状态。

        最关键的一条是 `est_out_ms` = 播放端排队深度 + 驱动侧输出延迟，也就是"音频进入
        播放端 → 耳机出声"。设备侧的打包与 USB 传输不在这个数里，面板会另算一行。

        **测不到的东西也必须说出来**：ffplay 不报告任何数字，所以那种模式 `measurable`
        为 False——面板不许把"没有数字"显示成"延迟很好"。
        """
        with self._lock:
            live = dict(self._live) if self._live is not None else None
            age = None if self._live_at is None else time.monotonic() - self._live_at
            failure = self._failure
            tail = list(self._stderr_tail)
        if live is None:
            return {
                "mode": self.mode,
                "measurable": False,
                "note": (
                    "播放端不报告排队深度（ffplay 没有播放位置接口）"
                    if self.mode == PLAYER_FFPLAY
                    else "播放端还没报出状态"
                ),
                "failure": failure,
            }
        result = {
            "mode": self.mode,
            "measurable": True,
            "age_s": None if age is None else round(age, 1),
            "stale": age is not None and age > SINK_STATS_STALE_S,
            "failure": failure,
            "stderr_tail": tail,
        }
        result.update(live)
        return result
