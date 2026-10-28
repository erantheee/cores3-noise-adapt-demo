"""驱动第三方播放器 system EQ 原型（tools/systemeq/systemeq）。

分工与 tools/host/player.py 一致：**S3 决策，Mac 执行**。区别是执行端不是
tools/player 自己解码本地文件，而是 tap 目标 App（QQ 音乐）的输出，过 24 段 EQ
再送当前耳机。这个模块是上位机侧的驱动：起进程、按白名单发命令、把自报状态交给快照。

口径（与 player.py 相同）：
  * `execution` 是回执不是愿望：进程在跑、有 PCM、非静音才报 `applied`。
  * 进程没构建/没启动如实报 `unavailable` 并带原因。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque

#: 允许透传给 systemeq 的命令。**白名单，不是通用终端**。`eq` 只允许上位机自己发。
ALLOWED_COMMANDS = {"eq", "flat", "bypass", "status", "mode"}
INTERNAL_COMMANDS = {"eq"}

NUMERIC_FIELDS = {"depth", "media_percent"}
BOOLEAN_FIELDS = {"on"}
STRING_FIELDS = {"value"}

MIN_APPLIED_GAIN_DB = -24.0
MAX_APPLIED_GAIN_DB = 24.0
#: 决策下发的节流，与 player.py 同一档。
DECISION_MIN_INTERVAL_S = 0.2
DECISION_MIN_DELTA_DB = 0.05
DECISION_MIN_DELTA_PERCENT = 1.0
#: tap 输入的 RMS 低于这个值视为没有信号（歌曲安静段不算"没在播"的极端情况仍可能误判，
#: 原型先按绝对门槛，后续按"最近 2.5 s 窗口最大值"更稳）。
SILENT_RMS = 0.0005


class SystemEqError(RuntimeError):
    """命令不合法或进程拒绝执行。原样带回界面，不吞。"""


class SystemEqService:
    def __init__(
        self,
        binary: str | None = None,
        app: str = "com.tencent.QQMusicMac",
        depth: float = 2.5,
        autostart: bool = True,
        demo: bool = True,
        mode: str = "aggressive",
    ) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.binary = binary or os.path.join(root, "systemeq", "systemeq")
        self.app = app
        self.depth = float(depth)
        self.demo = demo
        #: demo 基线档位：basic＝网友共识克制版，aggressive＝"demo 要明显"×2 版。
        self.mode = mode
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._ready: dict | None = None
        self._state_at: float = 0.0
        self._stderr_tail: list[str] = []
        self._reader: threading.Thread | None = None
        self._last_decision: tuple[float, list[float], float] = (0.0, [], 100.0)
        self._last_depth: float = 0.0
        self._last_base_gain: float = 1.0
        self._last_attempt = 0.0
        self.reason = ""
        self.last_error = ""
        #: 非空 = 进程因该原因主动退出（fail-open），自动重试被禁止，直到显式 start/重启。
        self._no_auto_restart: str | None = None
        #: 自动重启时间戳（60 秒窗口内最多 3 次，防抖循环）。
        self._restart_times: list[float] = []
        if autostart:
            self.start()

    @property
    def available(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        if not os.path.exists(self.binary):
            self.reason = f"systemeq 未构建（缺 {self.binary}）：先跑 sh tools/systemeq/build.sh"
            return False
        command = [self.binary, "--app", self.app, "--report", "1"]
        if self.demo:
            command.append("--demo")
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.reason = f"systemeq 启动失败：{exc}"
            return False
        self.reason = ""
        self._reader = threading.Thread(target=self._read_stdout, name="systemeq-out", daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, name="systemeq-err", daemon=True).start()
        # 进程起来就补一次档位，避免重启 host / 自动重试起进程后丢档。
        self._no_auto_restart = None
        try:
            self.command("mode", value=self.mode)
        except SystemEqError:
            pass
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
                continue
            if not isinstance(message, dict):
                continue
            with self._lock:
                if message.get("event") == "ready":
                    self._ready = message
                elif message.get("event") == "state":
                    self._state = message
                    self._state_at = time.monotonic()
                elif message.get("event") == "error":
                    self.reason = str(message.get("message", "systemeq 报错"))
                    self.last_error = self.reason
                elif message.get("event") == "quitting":
                    # 进程主动退出（如采样率不匹配 fail-open）。记住原因并禁止
                    # 5 秒自动重试把它再拉起来——否则 44.1 kHz 设备上会陷入
                    # "起→积压→退→起"的抖动循环。
                    reason = str(message.get("reason", "systemeq 退出"))
                    self.reason = reason
                    self.last_error = reason
                    if "采样率不匹配" in reason:
                        self._no_auto_restart = reason
                    else:
                        # ring 积压 / 设备配置变化等：允许自动重建，但 60 秒内最多 3 次。
                        now = time.monotonic()
                        self._restart_times = [t for t in self._restart_times
                                               if now - t < 60.0]
                        self._restart_times.append(now)
                        if len(self._restart_times) > 3:
                            self._no_auto_restart = (
                                f"{reason}（60 秒内已自动重启 3 次，停止重试）")

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            with self._lock:
                self._stderr_tail.append(line.rstrip())
                del self._stderr_tail[:-6]

    def _require_process(self):
        if not self.available or self._process is None or self._process.stdin is None:
            raise SystemEqError(self.reason or "systemeq 没有在运行")
        return self._process

    def _send(self, payload: dict) -> None:
        process = self._require_process()
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise SystemEqError(f"写入 systemeq 失败：{exc}") from exc

    def command(self, name: str, **fields) -> dict:
        if name not in ALLOWED_COMMANDS:
            raise SystemEqError(
                f"systemeq 不接受命令「{name}」；只能发：{', '.join(sorted(ALLOWED_COMMANDS))}"
            )
        payload: dict[str, object] = {"cmd": name}
        for key, value in fields.items():
            if key in NUMERIC_FIELDS:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise SystemEqError(f"字段 {key} 必须是数字")
                payload[key] = value
            elif key in BOOLEAN_FIELDS:
                if not isinstance(value, bool):
                    raise SystemEqError(f"字段 {key} 必须是布尔")
                payload[key] = value
            elif key in STRING_FIELDS:
                if not isinstance(value, str) or value not in ("basic", "aggressive"):
                    raise SystemEqError(f"字段 {key} 只接受 basic / aggressive")
                payload[key] = value
            else:
                raise SystemEqError(f"systemeq 不接受字段「{key}」")
        if name == "mode":
            self.mode = str(payload["value"])
        self._send(payload)
        return {"ok": True, "sent": payload}

    def set_depth(self, depth: float) -> dict:
        if not 1.0 <= depth <= 4.0:
            raise SystemEqError("深度倍率必须在 1.0…4.0 之间")
        self.depth = float(depth)
        return {"ok": True, "depth": self.depth}

    def apply_decision(self, bands: list[float], media_percent: float,
                       depth_override: float | None = None,
                       base_gain: float = 1.0) -> bool:
        if not self.available:
            # QQ 音乐没在出声时 tap 起不来、进程会退出。这里每隔几秒重试一次，
            # 用户点开播放后不用重启上位机就能恢复执行。
            now = time.monotonic()
            if now - self._last_attempt >= 5.0:
                self._last_attempt = now
                # 采样率不匹配时进程主动退出（fail-open）：不要自动把它拉起来，
                # 否则在 44.1 kHz 设备上会陷入"起→积压→退→起"的抖动循环。
                if not self._no_auto_restart:
                    self.start()
            self.last_error = self.reason or "systemeq 没有在运行"
            return False
        self._last_attempt = 0.0
        now = time.monotonic()
        last_at, last_bands, last_media = self._last_decision
        fresh = now - last_at >= DECISION_MIN_INTERVAL_S
        effective_depth = float(self.depth if depth_override is None else depth_override)
        same = (
            len(last_bands) == len(bands)
            and all(abs(a - b) < DECISION_MIN_DELTA_DB for a, b in zip(last_bands, bands))
            and abs(last_media - media_percent) < DECISION_MIN_DELTA_PERCENT
            and abs(self._last_depth - effective_depth) < 0.05
            and abs(self._last_base_gain - float(base_gain)) < 0.05
        )
        if same and not fresh:
            return False
        try:
            self._send(
                {
                    "cmd": "eq",
                    "bands": [float(value) for value in bands],
                    "depth": float(self.depth if depth_override is None else depth_override),
                    "media_percent": float(media_percent),
                    "base_gain": float(base_gain),
                }
            )
        except SystemEqError as exc:
            self.last_error = str(exc)
            return False
        self.last_error = ""
        self._last_decision = (now, [float(value) for value in bands], float(media_percent))
        self._last_depth = effective_depth
        self._last_base_gain = float(base_gain)
        return True

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state) if self._state else None
            ready = dict(self._ready) if self._ready else None
            reason = self.reason
            stderr_tail = list(self._stderr_tail)
            age = (time.monotonic() - self._state_at) if self._state else None

        if not self.available:
            return {
                "available": False,
                "reason": reason or "systemeq 没有在运行",
                "execution": "unavailable",
                "app": self.app,
                "demo": self.demo,
                "mode": self.mode,
                "depth": self.depth,
                "no_auto_restart_reason": self._no_auto_restart,
                "stderr_tail": stderr_tail,
                "last_error": self.last_error,
                "min_applied_gain_db": MIN_APPLIED_GAIN_DB,
                "max_applied_gain_db": MAX_APPLIED_GAIN_DB,
            }

        state = state or {}
        rms = state.get("rms")
        bypass = bool(state.get("bypass"))
        if bypass:
            execution = "bypass"
        elif isinstance(rms, (int, float)) and float(rms) <= SILENT_RMS:
            execution = "silent"
        elif isinstance(rms, (int, float)):
            execution = "applied"
        else:
            execution = "idle"

        applied = state.get("applied_bands") or []
        values = [float(value) for value in applied]
        lifted = [value for value in values if value > 0.05]
        cut = [value for value in values if value < -0.05]

        return {
            "available": True,
            "reason": reason,
            "execution": execution,
            "app": self.app,
            "demo": self.demo,
            "mode": state.get("mode") or self.mode,
            "bypass": bypass,
            "depth": state.get("depth", self.depth),
            "media_percent": state.get("media_percent"),
            "rms": rms,
            "peak": state.get("peak"),
            "ring_fill": state.get("ring_fill"),
            "callbacks": state.get("callbacks"),
            "frames": state.get("frames"),
            "consumed_frames": state.get("consumed_frames"),
            "sample_rate": state.get("sample_rate") or (ready or {}).get("sample_rate"),
            #: 消费端是否在做重采样（tap 采样率 != 输出设备采样率时，2026-10-23）。
            "resampling": (ready or {}).get("resampling"),
            "engine_rate": (ready or {}).get("engine_rate"),
            #: Mac 侧内容特征（方案 A）：harshness 0=稀疏人声（保留高频）、1=密集器乐（收高频）。
            "content_harshness": state.get("content_harshness"),
            "content_high_ratio": state.get("content_high_ratio"),
            "content_mid_ratio": state.get("content_mid_ratio"),
            "content_fed_frames": state.get("content_fed_frames"),
            "content_analyses": state.get("content_analyses"),
            "content_window_rms": state.get("content_window_rms"),
            "content_fill": state.get("content_fill"),
            "out_ring_fill": state.get("out_ring_fill"),
            "content_env_exposure": state.get("content_env_exposure"),
            "content_trim": state.get("content_trim"),
            #: EQ **之后**的输出峰值与削波计数（2026-10-28）：判断"人声毛刺"是不是
            #: 削波造成的，只能量 EQ 之后的信号；tap 的 peak 在 EQ 之前。
            "out_peak": state.get("out_peak"),
            "out_clip_frames": state.get("out_clip_frames"),
            "out_frames": state.get("out_frames"),
            #: 输出削波时对正增益动态部分的收缩量（0…2 dB）。
            "headroom_trim_db": state.get("headroom_trim_db"),
            #: 设备建议（S3 原始 24 段）与 systemeq 真正设进 EQ 的值（含 demo 基线 + ×depth）。
            "source_bands": state.get("bands") or [],
            "applied_bands": values,
            "applied_peak_abs_db": round(max((abs(v) for v in values), default=0.0), 2),
            "applied_max_cut_db": round(min(cut), 2) if cut else 0.0,
            "applied_lifted_bands": len(lifted),
            "applied_cut_bands": len(cut),
            "state_age_s": None if age is None else round(age, 2),
            "stderr_tail": stderr_tail,
            "last_error": self.last_error,
            "min_applied_gain_db": MIN_APPLIED_GAIN_DB,
            "max_applied_gain_db": MAX_APPLIED_GAIN_DB,
        }
