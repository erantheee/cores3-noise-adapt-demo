"""Low-latency playback of CoreS3 microphone PCM through macOS."""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass


class RelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayStats:
    chunks_written: int
    chunks_dropped: int
    bytes_written: int
    #: 因为超出延迟预算而被丢掉的旧音频。与 chunks_dropped 分开报：那一个是"我们的队列
    #: 满了"，这一个是"播放器已经囤够了，再喂只会让声音更晚出来"。
    chunks_stale: int


class AudioRelay:
    """Send 16 kHz mono PCM to the current macOS default output.

    Serial parsing never blocks on playback. If the host falls behind, the oldest queued
    sound is discarded instead of allowing audible latency to grow without bound.
    """

    #: 允许播放器最多比"现在"多囤多久的音频。
    #:
    #: 这个数是整条链路的延迟上限，不是优化参数。实时流的产生速率和播放速率相同，所以
    #: 任何一次抖动造成的积压都会**永久保留**——不会自己退回去。实测：ffplay 在无节流写入
    #: 下 1.08 秒吃掉 5 秒音频（吸收比 4.6×），它不提供反压，于是我们这边的队列永远不满、
    #: 永远不丢，积压全在它内部生长。加了预算之后，超出的旧音频在进入播放器之前就被丢掉，
    #: 声音宁可断一下，也不会越听越晚。
    #:
    #: 0.10 s 的取舍：更小会削到连续说话的开头（设备侧是 320 点 = 20 ms 一块），更大则把
    #: 下限推高。真正不可压缩的是播放器自己那 ~64 ms 的 SDL 缓冲与 AirPods 的蓝牙输出。
    DEFAULT_BUDGET_SECONDS = 0.10

    def __init__(self, rate: int = 16000, queue_chunks: int = 2,
                 budget_seconds: float = DEFAULT_BUDGET_SECONDS,
                 player_command: list[str] | None = None) -> None:
        self.rate = rate
        self.budget_bytes = int(rate * 2 * budget_seconds)
        #: 留出注入点，测试里用 `cat` 当"照单全收"的播放器，就不需要声卡也能验证预算。
        self._player_command = player_command
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_chunks)
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._chunks_written = 0
        self._chunks_dropped = 0
        self._bytes_written = 0
        self._chunks_stale = 0
        self._committed_bytes = 0
        self._play_clock: float | None = None
        self._failure: str | None = None

    def start(self) -> None:
        command = self._player_command
        if command is None:
            ffplay = shutil.which("ffplay")
            if ffplay is None:
                raise RelayError("找不到 ffplay；请先安装 ffmpeg（brew install ffmpeg）")
            command = [
                ffplay, "-nodisp", "-loglevel", "error",
                "-fflags", "nobuffer", "-flags", "low_delay", "-avioflags", "direct",
                "-probesize", "32", "-analyzeduration", "0",
                "-f", "s16le", "-ar", str(self.rate), "-ch_layout", "mono",
                # CoreS3 的麦克风电平刻意保守，实测板上约 -40 dB RMS。先把它抬到能听清，
                # 再用限幅器压住峰值，而不是要求用户把 AirPods 音量拉满。
                "-af", "volume=18dB,alimiter=limit=0.95",
                "-i", "pipe:0",
            ]
        # 这里曾经设过 SDL_AUDIO_MIN_BUFFER_SIZE——那是无效的：SDL2 不读这个变量，ffplay 自己
        # 按 `sample_rate / 30` 左右决定 SDL 回调缓冲（16 kHz 时约 1024 点 = 64 ms），命令行
        # 改不动它。所以"把播放器缓冲调小"在 ffplay 上做不到，延迟只能靠上游丢旧音频来压。
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE)
        self._thread = threading.Thread(target=self._run, name="audio-relay", daemon=True)
        self._thread.start()

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
        try:
            while True:
                pcm = self._queue.get()
                if pcm is None:
                    break
                # 只允许领先实际播放进度 budget_bytes。第一个块到达时定下播放时钟：这一刻
                # 之前的音频都还没播出去，预算就是给它起播用的那一点余量。
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

    @property
    def failure(self) -> str | None:
        return self._failure

    def stats(self) -> RelayStats:
        with self._lock:
            return RelayStats(
                self._chunks_written,
                self._chunks_dropped,
                self._bytes_written,
                self._chunks_stale,
            )
