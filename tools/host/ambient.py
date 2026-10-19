"""环境声执行端：把固件的 `ambient_open` 意图落到 AirPods 聆听模式上。

分工：**S3 决定该不该让现实声音进来，Mac 负责执行**。设备本身没有"打开耳朵"的能力
（AirPods 不开放通透 DSP 的控制接口），macOS 也没有公开 API 切聆听模式；兄弟工程
`airpods-control` 的 `bin/airpods mode ...` 走辅助功能（AX）点控制中心声音面板，实测可用，
所以这里只负责驱动它，并把"意图"与"执行结果"分开上报。

三条口径，缺一条都会在演示现场骗人：

1. **执行失败必须如实报错。** 意图 true 但 AirPods 没切过去时，界面要显示"意图已发出、
   执行失败"，不能显示成"环境声已开放"。
2. **切换前先读当前模式，关闭时切回去。** 用户可能开着降噪；对话结束不能把人留在通透。
3. **不阻塞控制循环。** AX 切换要开合控制中心，耗时在秒级，放在 100 ms 的控制/渲染循环里
   会把面板卡死。`set_open()` 只置目标，工作线程串行执行；同一目标不重复切。

在没有 AirPods / 没有 AX 权限 / CLI 不存在时，本模块保持"不可用"，如实上报原因。
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

#: 打开环境声时切到的聆听模式（CLI 的参数名）。
DEFAULT_OPEN_MODE = "transparency"
#: 控制中心面板里的中文模式名 -> airpods CLI 参数。
MODE_ARGS = {
    "降噪": "anc",
    "通透模式": "transparency",
    "自适应": "adaptive",
    "anc": "anc",
    "transparency": "transparency",
    "adaptive": "adaptive",
}
#: 失败后的重试间隔。AX 失败常见于权限/面板没开出来，100 ms 重试只会刷屏。
RETRY_BACKOFF_S = 3.0


class AmbientError(RuntimeError):
    """AirPods 聆听模式读取/切换失败。原因原样带回界面，不吞。"""


class AmbientService:
    """环境声开关的异步执行端。

    `set_open()` 是唯一入口；它只记录目标并唤醒工作线程。`snapshot()` 可以在任意线程调用，
    给面板一份"目标 / 实际 / 上次错误"的快照。测试用 `autostart=False` 建实例后直接调
    `step()`，不依赖线程与真实时钟。
    """

    def __init__(
        self,
        cli_path: str | Path,
        open_mode: str = DEFAULT_OPEN_MODE,
        enabled: bool = True,
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
        timeout_s: float = 12.0,
        autostart: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cli_path = Path(cli_path)
        self.open_mode = open_mode if open_mode in MODE_ARGS.values() else DEFAULT_OPEN_MODE
        self.enabled = enabled
        self.timeout_s = timeout_s
        self._runner = runner
        self._clock = clock

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._target_open = False
        self._open_applied = False
        self._previous_mode: str | None = None
        self._last_error = ""
        self._last_change_at: float | None = None
        self._last_attempt_at: float | None = None
        self._thread: threading.Thread | None = None

        if autostart:
            self._thread = threading.Thread(target=self._run, name="ambient-airpods", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ 生命周期

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.cli_path.exists())

    def set_open(self, want: bool) -> None:
        """请求"环境声开放/关闭"。非阻塞；同一目标不会重复执行。"""
        with self._lock:
            self._target_open = bool(want)
        self._wake.set()

    def stop(self, timeout_s: float = 6.0) -> None:
        """停止工作线程。若环境声还开着，尽量先切回去。"""
        try:
            self.set_open(False)
            if self._open_applied and self.available:
                self._do_close()
        except AmbientError as exc:  # 停止路径不抛给调用方，但要留下痕迹
            with self._lock:
                self._last_error = f"退出时恢复聆听模式失败：{exc}"
        finally:
            self._stop.set()
            self._wake.set()
            if self._thread is not None:
                self._thread.join(timeout=timeout_s)

    # ------------------------------------------------------------------ 工作线程

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.step()

    def step(self) -> None:
        """执行一次目标检查（工作线程每 0.5 s 调一次；测试直接调）。"""
        with self._lock:
            want = self._target_open
            applied = self._open_applied
        if not self.enabled:
            return
        if want == applied:
            return
        if not self.available:
            with self._lock:
                self._last_error = f"airpods CLI 不存在或未启用：{self.cli_path}"
            return
        now = self._clock()
        if (
            self._last_error
            and self._last_attempt_at is not None
            and now - self._last_attempt_at < RETRY_BACKOFF_S
        ):
            return
        with self._lock:
            self._last_attempt_at = now
        try:
            if want:
                self._do_open()
            else:
                self._do_close()
        except AmbientError as exc:
            with self._lock:
                self._last_error = str(exc)

    # ------------------------------------------------------------------ 具体动作

    def _do_open(self) -> None:
        previous = self._read_listening_mode()
        with self._lock:
            self._previous_mode = previous
        if previous != self.open_mode:
            self._run_cli(["mode", self.open_mode])
        with self._lock:
            self._open_applied = True
            self._last_error = ""
            self._last_change_at = self._clock()

    def _do_close(self) -> None:
        with self._lock:
            previous = self._previous_mode
        if previous is not None and previous != self.open_mode:
            self._run_cli(["mode", previous])
        with self._lock:
            self._open_applied = False
            self._previous_mode = None
            self._last_error = ""
            self._last_change_at = self._clock()

    def _read_listening_mode(self) -> str:
        completed = self._run_cli(["status"])
        text = "\n".join(
            [completed.stdout or "", completed.stderr or ""]
        )
        for line in text.splitlines():
            if "聆听模式：" not in line:
                continue
            value = line.split("聆听模式：", 1)[1].strip()
            if value in MODE_ARGS:
                return MODE_ARGS[value]
        last = next((line.strip() for line in text.splitlines() if line.strip()), "")
        raise AmbientError(f"读不到 AirPods 聆听模式：{last or '没有输出'}")

    def _run_cli(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        argv = [str(self.cli_path), *args]
        try:
            if self._runner is not None:
                completed = self._runner(argv)
            else:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AmbientError(f"执行 airpods {' '.join(args)} 失败：{exc}") from exc
        if completed.returncode != 0:
            output = "\n".join(
                part.strip() for part in (completed.stdout, completed.stderr) if part
            ).strip()
            last = output.splitlines()[-1] if output else "没有输出"
            raise AmbientError(
                f"airpods {' '.join(args)} 退出码 {completed.returncode}：{last}"
            )
        return completed

    # ------------------------------------------------------------------ 读出

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "available": self.available,
                "open": self._open_applied,
                "target": self._target_open,
                "open_mode": self.open_mode if self._open_applied else None,
                "previous_mode": self._previous_mode,
                "last_error": self._last_error,
                "last_change_s": None
                if self._last_change_at is None
                else round(self._clock() - self._last_change_at, 1),
                "executor": "airpods-control",
            }


class RelayAmbient:
    """环境声执行端（当前 demo 路径）：**S3 麦克风 → Mac → 耳机** 中继。

    与 `AmbientService` 同一套鸭子接口（`set_open` / `snapshot` / `stop` / `available`），
    但这里不做聆听模式切换，只做**门控**：S3 已经在持续推 16 kHz 麦克风音频，S3 说"该开放"
    就把后续包放行给中继播放端，说"关闭"就丢弃。

    为什么关着也让设备继续推流：打开延迟就是这条玩法要暴露的量。等 S3 说开、再给设备发
    `rec start`、再等 20 ms 一包的音频，起播会再晚几百毫秒；持续推流 + 本地门控把
    "打开"压到一包音频 + 播放端排队。**代价是这条通路的固有时延**（实测约 211–231 ms，
    主要是蓝牙输出 170 ms），所以它先验证"能不能预判 + 能不能开"，不宣称"能听清对面"。

    设备端是否在推流由上位机启动时发的 `rec start` 决定（见 `__main__.py`），本类不碰串口。
    """

    def __init__(self, relay: object, enabled: bool = True) -> None:
        self.relay = relay
        self.enabled = enabled
        self._lock = threading.RLock()
        self._open = False
        self._target = False
        self._fed_chunks = 0
        self._dropped_chunks = 0
        self._opens = 0
        self._last_change_at: float | None = None

    # ------------------------------------------------------------------ 接口

    @property
    def available(self) -> bool:
        if not self.enabled or self.relay is None:
            return False
        return not bool(getattr(self.relay, "failure", None))

    def set_open(self, want: bool) -> None:
        """请求开放/关闭环境声。非阻塞；只改门控，不碰设备与播放端。"""
        with self._lock:
            want = bool(want)
            if want == self._target:
                return
            self._target = want
            self._open = want
            if want:
                self._opens += 1
            self._last_change_at = time.monotonic()

    def feed(self, pcm: bytes, rate: int) -> None:
        """由数据源线程对每个麦克风包调用一次。关闭时丢弃，不排队。"""
        with self._lock:
            open_now = self._open
            if not open_now:
                self._dropped_chunks += 1
                return
            self._fed_chunks += 1
        self.relay.feed(pcm, rate)

    def stop(self) -> None:
        self.set_open(False)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            failure = getattr(self.relay, "failure", None) if self.relay is not None else "中继未创建"
            return {
                "enabled": self.enabled,
                "available": self.available,
                "open": self._open,
                "target": self._target,
                # 与 AirPods 执行端保持同一份字段名，面板不用为执行端写两套逻辑。
                "open_mode": "s3-relay",
                "previous_mode": None,
                "last_error": failure or "",
                "last_change_s": None
                if self._last_change_at is None
                else round(time.monotonic() - self._last_change_at, 1),
                "executor": "s3-relay",
                "chunks_fed": self._fed_chunks,
                "chunks_dropped": self._dropped_chunks,
                "opens": self._opens,
            }
