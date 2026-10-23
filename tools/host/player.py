"""驱动 Mac 播放与执行端（tools/player/player）。

分工见 docs/product-alignment.md：**S3 决策，Mac 执行**。这个模块是"执行"那半边的上位机侧：
起播放器子进程、按白名单发命令、把播放器自报的状态原样交给快照。

为什么是子进程 + JSON 行：macOS 上能做实时多段 EQ 的只有 AVAudioEngine，而 Python 这边没有
PyObjC。所以播放器是 swiftc 编译出来的命令行程序（tools/player/），这里只做管道。

口径（与固件/文档一致，不许含糊）：
  * `execution` 是**回执**，不是愿望：只有在"引擎在跑 + 正在播放 + EQ 已设 + 实测峰值不为静音"
    时才报 `applied`。没在播放就是 `idle`——设备决策已产生但没人执行，界面必须能看出这个差别。
  * 播放器没构建出来时如实报 `unavailable` 并带上原因，不假装在播。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque

#: 允许透传给播放器的命令。**白名单，不是通用终端**——与设备回控同一条原则：面板只能做
#: 演示需要的那几件事，别的输入一律拒绝。
ALLOWED_COMMANDS = {
    "play",
    "pause",
    "toggle",
    "next",
    "prev",
    "select",
    "seek",
    "volume",
    "bypass",
    "status",
}

#: **只允许上位机自己发**的命令。`eq` 不在面板白名单里，是故意的：EQ 的形状必须来自设备
#: 那一帧，面板只能调深度倍率，不能自己造一条曲线——否则"谁在做决策"这个唯一变量就没了。
#: 2026-10-14 的教训：这条路一开始漏了白名单，`apply_decision` 每 100 ms 抛
#: PlayerError 又被 `except` 吞掉，表现是"面板一切正常、曲线恒为 0"——**静默失败最贵**。
INTERNAL_COMMANDS = {"eq"}

#: 这些字段是数字/布尔，直接透传；其余字段一律拒绝，避免把任意 JSON 塞进播放器。
NUMERIC_FIELDS = {"index", "seconds", "percent", "depth"}
BOOLEAN_FIELDS = {"on", "autoplay"}

DEFAULT_VOLUME_PERCENT = 150.0
#: 播放器真正能执行的逐段增益范围（dB），逐字对应 tools/player/player.swift 的
#: `minAppliedGainDb = -12` / `maxAppliedGainDb = +10`（按深度放大后再钳位）。
#: 这里镜像一份是为了让**快照自己说得出**面板该按什么量程画：面板不能把 -12…+10
#: 当经验值写死在 JS 里；旧 host 的快照没有这个字段时面板才退回同一组常量。
MIN_APPLIED_GAIN_DB = -12.0
MAX_APPLIED_GAIN_DB = 10.0
#: 判断"这一段算抬还是算削"的门限，与协议层 >0.05 / <-0.05 的口径一致。
SIGNIFICANT_GAIN_DB = 0.05
#: 决策下发的节流：设备 10 Hz、播放器 20 ms 一次斜坡，按帧发没有意义，只会刷屏。
DECISION_MIN_INTERVAL_S = 0.2
DECISION_MIN_DELTA_DB = 0.05
DECISION_MIN_DELTA_PERCENT = 1.0
#: 判"在播但其实没出声"的窗口与门槛。真实歌曲有安静段，单点峰值不能当判据：
#: 窗口内最大值仍低于这个门槛，才算"确实没声音出来"。
PEAK_WINDOW_S = 2.5
SILENT_PEAK_DB = -70.0


class PlayerError(RuntimeError):
    """命令不合法或播放器拒绝执行。原样带回界面，不吞。"""


class PlayerService:
    def __init__(
        self,
        binary: str | None = None,
        tracks: str | None = None,
        volume_percent: float = DEFAULT_VOLUME_PERCENT,
        autostart: bool = True,
    ) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        player_dir = os.path.join(root, "player")
        self.binary = binary or os.path.join(player_dir, "player")
        self.tracks_path = tracks or os.path.join(player_dir, "tracks.json")
        self.volume_percent = float(volume_percent)
        self.depth = 2.5
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._state_at: float = 0.0
        self._ready: dict | None = None
        self._stderr_tail: list[str] = []
        self._reader: threading.Thread | None = None
        self._last_decision: tuple[float, list[float], float] = (0.0, [], 0.0)
        #: 实测峰值的滑动窗口。用**单点**峰值判"没出声"会误判：真实歌曲有安静段
        #: （《渡口》开头就掉到 -120 dB），2026-10-14 实测把正常播放报成 silent。
        self._peak_window: deque[tuple[float, float]] = deque(maxlen=200)
        self.reason = ""
        #: 最近一次决策下发失败的原因。**必须显示出来**：这条通路曾经静默失败过
        #: （eq 不在白名单里 → 每帧抛异常被吞 → 面板全绿而曲线恒为 0）。
        self.last_error = ""
        if autostart:
            self.start()

    # ------------------------------------------------------------------ 生命周期

    @property
    def available(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        if not os.path.exists(self.binary):
            self.reason = (
                f"播放器未构建（缺 {self.binary}）：先跑 sh tools/player/build.sh"
            )
            return False
        if not os.path.exists(self.tracks_path):
            self.reason = f"缺曲库文件 {self.tracks_path}"
            return False
        try:
            self._process = subprocess.Popen(
                [self.binary, "--tracks", self.tracks_path, "--volume", str(self.volume_percent)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.reason = f"播放器启动失败：{exc}"
            return False
        self.reason = ""
        self._reader = threading.Thread(target=self._read_stdout, name="player-out", daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, name="player-err", daemon=True).start()
        return True

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                process.stdin.flush()
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # 播放器的非协议输出不该污染状态，也不该被当成状态
            if not isinstance(message, dict):
                continue
            with self._lock:
                if message.get("event") == "ready":
                    self._ready = message
                elif message.get("event") == "state":
                    self._state = message
                    self._state_at = time.monotonic()
                    peak = message.get("peak_db")
                    if isinstance(peak, (int, float)):
                        self._peak_window.append((self._state_at, float(peak)))
                        cutoff = self._state_at - PEAK_WINDOW_S
                        while self._peak_window and self._peak_window[0][0] < cutoff:
                            self._peak_window.popleft()
                elif message.get("event") == "error":
                    self.reason = str(message.get("message", "播放器报错"))

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            with self._lock:
                self._stderr_tail.append(line.rstrip())
                del self._stderr_tail[:-6]

    # ------------------------------------------------------------------ 命令

    def _require_process(self):
        if not self.available or self._process is None or self._process.stdin is None:
            raise PlayerError(self.reason or "播放器没有在运行")
        return self._process

    def _send(self, payload: dict) -> None:
        process = self._require_process()
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise PlayerError(f"写入播放器失败：{exc}") from exc

    def command(self, name: str, **fields) -> dict:
        """面板可发的命令。白名单之外的命令与字段一律拒绝。"""
        if name not in ALLOWED_COMMANDS:
            raise PlayerError(
                f"播放器不接受命令「{name}」；面板只能发：{', '.join(sorted(ALLOWED_COMMANDS))}"
            )
        payload: dict[str, object] = {"cmd": name}
        for key, value in fields.items():
            if key in NUMERIC_FIELDS:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise PlayerError(f"字段 {key} 必须是数字")
                payload[key] = value
            elif key in BOOLEAN_FIELDS:
                if not isinstance(value, bool):
                    raise PlayerError(f"字段 {key} 必须是布尔")
                payload[key] = value
            else:
                raise PlayerError(f"播放器不接受字段「{key}」")
        if name == "select" and "index" in payload:
            count = (self._state or {}).get("track_count") or 0
            if count and not 0 <= int(payload["index"]) < int(count):
                raise PlayerError(f"曲目序号 {payload['index']} 超出范围（0…{int(count) - 1}）")
        if name == "volume" and "percent" in payload:
            percent = float(payload["percent"])
            if not 0.0 <= percent <= 200.0:
                raise PlayerError("播放增益必须在 0…200% 之间")
            self.volume_percent = percent
        self._send(payload)
        return {"ok": True, "sent": payload}

    def set_depth(self, depth: float) -> dict:
        """设置 Mac 处方深度。形状来自 S3，深度是本机的演示参数，面板上必须写明。"""
        if not 1.0 <= depth <= 4.0:
            raise PlayerError("深度倍率必须在 1.0…4.0 之间")
        self.depth = float(depth)
        return {"ok": True, "depth": self.depth}

    def apply_decision(self, bands: list[float], media_percent: float) -> bool:
        """把一帧 S3 决策下发给播放器。节流后返回是否真的发了。

        走 `_send` 而不是 `command`：`eq` 只允许上位机发，面板不能自己造曲线。
        失败的**原因必须被记住**（last_error）：静默吞掉异常会让面板看起来一切正常、
        而曲线恒为 0，2026-10-14 就是这么踩了一次。
        """
        if not self.available:
            self.last_error = self.reason or "播放器没有在运行"
            return False
        now = time.monotonic()
        last_at, last_bands, last_media = self._last_decision
        fresh = now - last_at >= DECISION_MIN_INTERVAL_S
        same = (
            len(last_bands) == len(bands)
            and all(abs(a - b) < DECISION_MIN_DELTA_DB for a, b in zip(last_bands, bands))
            and abs(last_media - media_percent) < DECISION_MIN_DELTA_PERCENT
        )
        if same and not fresh:
            return False
        try:
            self._send(
                {
                    "cmd": "eq",
                    "bands": [float(value) for value in bands],
                    "depth": self.depth,
                    "media_percent": float(media_percent),
                }
            )
        except PlayerError as exc:
            self.last_error = str(exc)
            return False
        self.last_error = ""
        self._last_decision = (now, [float(value) for value in bands], float(media_percent))
        return True

    # ------------------------------------------------------------------ 读出

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state) if self._state else None
            ready = dict(self._ready) if self._ready else None
            reason = self.reason
            stderr_tail = list(self._stderr_tail)
            age = (time.monotonic() - self._state_at) if self._state else None
            window_peak = max((peak for _, peak in self._peak_window), default=None)

        if not self.available:
            return {
                "available": False,
                "reason": reason or "播放器没有在运行",
                "stderr_tail": stderr_tail,
                "execution": "unavailable",
                "depth": self.depth,
                "volume_percent": self.volume_percent,
                "min_applied_gain_db": MIN_APPLIED_GAIN_DB,
                "max_applied_gain_db": MAX_APPLIED_GAIN_DB,
            }

        state = state or {}
        playing = bool(state.get("playing"))
        bypass = bool(state.get("bypass"))
        peak_db = state.get("peak_db")
        applied = state.get("applied_bands") or []
        # 有符号调音（负=削）：`applied_peak_db` 仍是"各段里的最大值"（旧字段含义不变；
        # 全为削时它是负的那个最大值）。新增的几个字段把"抬多少/削多少"分开报出来，
        # 面板据此写清数字，而不是自己从数组里猜——但数组本身就是播放器的实测，
        # 面板拿它兜底展示也是"读实测"，不是造曲线。
        lifted_applied = [value for value in applied if value > SIGNIFICANT_GAIN_DB]
        cut_applied = [value for value in applied if value < -SIGNIFICANT_GAIN_DB]
        applied_peak_abs = max((abs(value) for value in applied), default=None)
        applied_max_cut = min(cut_applied) if cut_applied else 0.0
        # 回执的判据写死在这里：不在播就不算执行；直通时也不算；确实没出声同样不算。
        # "没出声"用**窗口内最大值**判断，不用瞬时值——真实歌曲有安静段（《渡口》开头
        # 就掉到 -120 dB，单点判据会把正常播放报成 silent，2026-10-14 实测撞到）。
        if bypass:
            execution = "bypass"
        elif not playing:
            execution = "idle"
        elif isinstance(window_peak, float) and window_peak <= SILENT_PEAK_DB:
            execution = "silent"
        elif window_peak is None and isinstance(peak_db, (int, float)) and peak_db <= SILENT_PEAK_DB:
            execution = "silent"
        else:
            execution = "applied"

        return {
            "available": True,
            "reason": reason,
            "execution": execution,
            "playing": playing,
            "bypass": bypass,
            "track_index": state.get("track_index"),
            "track_title": state.get("track_title", ""),
            "track_count": state.get("track_count", 0),
            "position_s": state.get("position_s"),
            "duration_s": state.get("duration_s"),
            "volume_percent": state.get("volume_percent", self.volume_percent),
            "depth": state.get("depth", self.depth),
            "media_percent": state.get("media_percent"),
            "source_bands": state.get("source_bands") or [],
            "applied_bands": applied,
            #: 旧字段：各段里的最大值。有符号曲线全为削时它会是负数——语义没变。
            "applied_peak_db": max(applied) if applied else None,
            #: 有符号口径（新增）：绝对值最大的那一段、最大削减（负数或 0）、抬/削段数。
            "applied_peak_abs_db": None if applied_peak_abs is None else round(applied_peak_abs, 2),
            "applied_max_cut_db": round(applied_max_cut, 2),
            "applied_lifted_bands": len(lifted_applied),
            "applied_cut_bands": len(cut_applied),
            #: 面板画图用的有符号量程。上限优先用播放器自报值（ready 消息），没有就用契约值。
            "min_applied_gain_db": MIN_APPLIED_GAIN_DB,
            "max_applied_gain_db": (ready or {}).get("max_applied_gain_db", MAX_APPLIED_GAIN_DB),
            "peak_db": peak_db,
            #: 最近 PEAK_WINDOW_S 秒里的最大峰值。判"有没有出声"看这个，不看瞬时值。
            "peak_window_db": window_peak,
            "output_device": state.get("output_device") or (ready or {}).get("output_device", ""),
            "sample_rate": state.get("sample_rate"),
            "state_age_s": None if age is None else round(age, 2),
            #: 播放器因输出设备切换而重启音频引擎的次数。>0 不一定是故障——它说明
            #: "换个耳机/拔掉耳机后播放没有卡死"这条恢复路径真的走过。
            "recoveries": state.get("recoveries", 0),
            "tracks": (ready or {}).get("tracks", []),
            "stderr_tail": stderr_tail,
            "last_error": self.last_error,
        }
