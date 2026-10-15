"""Low-latency playback of CoreS3 microphone PCM through macOS."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass


class RelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayStats:
    chunks_written: int
    chunks_dropped: int
    bytes_written: int


class AudioRelay:
    """Send 16 kHz mono PCM to the current macOS default output.

    Serial parsing never blocks on playback. If the host falls behind, the oldest queued
    sound is discarded instead of allowing audible latency to grow without bound.
    """

    def __init__(self, rate: int = 16000, queue_chunks: int = 2) -> None:
        self.rate = rate
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_chunks)
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._chunks_written = 0
        self._chunks_dropped = 0
        self._bytes_written = 0
        self._failure: str | None = None

    def start(self) -> None:
        ffplay = shutil.which("ffplay")
        if ffplay is None:
            raise RelayError("找不到 ffplay；请先安装 ffmpeg（brew install ffmpeg）")
        command = [
            ffplay, "-nodisp", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay", "-avioflags", "direct",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "s16le", "-ar", str(self.rate), "-ch_layout", "mono",
            # CoreS3's microphone stream is intentionally conservative and measured
            # around -40 dB RMS on the real board. Bring speech to an audible level,
            # then limit peaks instead of asking the user to max out AirPods volume.
            "-af", "volume=18dB,alimiter=limit=0.95",
            "-i", "pipe:0",
        ]
        env = os.environ.copy()
        # ffplay uses SDL for output. Request a small callback buffer; stale speech is worse
        # than an occasional drop in a live hear-through path.
        env["SDL_AUDIO_MIN_BUFFER_SIZE"] = "128"
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE, env=env)
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
                self._process.stdin.write(pcm)
                self._process.stdin.flush()
                with self._lock:
                    self._chunks_written += 1
                    self._bytes_written += len(pcm)
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
            return RelayStats(self._chunks_written, self._chunks_dropped, self._bytes_written)
