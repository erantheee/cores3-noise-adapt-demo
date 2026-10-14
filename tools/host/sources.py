"""上位机的数据源。

三个来源，同一个出口（一行一行的串口文本）。这样界面、状态归约、记录三者完全不需要
知道数据是从哪来的，也就能在没有设备的情况下把整条链路跑通并测出来。

    serial   真机串口
    replay   历史日志回放（把一次真有设备在场的会话搬回桌面上复核）
    mock     模拟源：按固件的真实行为生成帧，**并明确标注它不是设备**

一条纪律：**mock 永远不冒充设备。** 界面顶部会如实显示来源，`link` 字段一律保持
`simulated`——README 已经写死"没有确认回执前不得把 simulated 改成已连接或已发送"，
这一条对模拟数据源同样成立。
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .protocol import iter_json_objects
from .simulating import MockDevice
from .state import HostState

#: 端口打开后多久没有数据就放弃。项目的既有判据是 10 秒（tools/drift_capture.py）。
NO_DATA_TIMEOUT_S = 10.0
#: 打开串口后，对**内置 USB Serial/JTAG**要置位 DTR/RTS，否则本机收不到任何字节
#: （实测：不置位 10 秒 0 字节，置位 10 秒 17 kB）。
#:
#: 但**不能对所有端口都这样**：在经典 USB-UART 桥（CP2102/CH340/FTDI，DTR→IO0、
#: RTS→EN）上，RTS 置位等于把 EN 持续拉低，芯片会一直被按住复位——症状和这个 bug
#: 一模一样（读到 0 字节）。所以只有确认是内置 USB-Serial/JTAG 才置位。
ESP32S3_DTR = True
ESP32S3_RTS = True


def is_native_usb_jtag_port(port: str) -> bool:
    """判断这个端口是不是 ESP32-S3 的内置 USB Serial/JTAG。

    内置 USB-Serial/JTAG 在 macOS 上的节点名就是 `/dev/cu.usbmodem*`（VID 0x303A）。
    经典 USB-UART 桥（CP2102/CH340/FTDI）会出现在别的前缀下，例如 `/dev/cu.usbserial*`
    或 `/dev/cu.SLAB_USBtoUART`，因此按前缀区分在本机是可靠的。

    判不出来时返回 False，也就是**不碰 modem 线**——宁可"本机读不到数据"（有明确报错），
    也不要把一块好板子按在复位里。真要在别的平台上用，得先确认那条串口是不是内置 JTAG。
    """
    return port.startswith("/dev/cu.usbmodem") or port.startswith("/dev/tty.usbmodem")


#: 优先匹配的设备节点。USB Serial/JTAG 在 macOS 上就是 usbmodem 这一族。
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/tty.usbmodem*")


class MissingDependency(Exception):
    """缺少 pyserial 之类的运行依赖。要明确报缺，不给假结果。"""


class SourceError(Exception):
    """数据源无法启动。消息会被原样展示给使用者，所以要写清楚怎么办。"""


@dataclass
class DevicePresence:
    present: bool
    detail: str
    #: True 表示这次判定来自 USB 设备树；False 表示只是猜的（无 ioreg）。
    authoritative: bool


def usb_presence() -> DevicePresence:
    """查 USB 设备树里有没有 Espressif 设备。

    为什么值得单独查一遍：**`/dev/cu.usbmodem*` 节点在设备拔掉后仍然存在**（项目文档
    已记录）。端口能打开、`ls` 能看到，但一个字节都读不到。所以"节点在不在"不能用来
    判断设备在不在；USB 设备树可以。

    非 macOS 或取不到时返回 authoritative=False，调用方会退回"打开端口后看有没有数据"。
    """
    if sys.platform != "darwin":
        return DevicePresence(False, f"非 macOS（{sys.platform}），跳过 USB 设备树检查", False)
    try:
        result = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-w0", "-l"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DevicePresence(False, f"ioreg 不可用：{exc}", False)
    if result.returncode != 0:
        return DevicePresence(False, f"ioreg 退出码 {result.returncode}", False)

    text = result.stdout
    if "Espressif" in text or '"idVendor" = 12346' in text:
        names = [
            line.split("=", 1)[1].strip().strip('"')
            for line in text.splitlines()
            if "USB Product Name" in line and "Espressif" in text
        ]
        return DevicePresence(True, f"USB 设备树里有 Espressif 设备（{names[0] if names else '已枚举'}）", True)
    return DevicePresence(
        False, "USB 设备树里没有 Espressif 设备（CoreS3 没插，或插在只供电的口/转接器上）", True
    )


def candidate_ports() -> list[tuple[str, bool]]:
    """列出候选串口，附带"是否像当前存在的设备节点"的标记。

    macOS 上 `/dev/cu.*` 会为已经不存在的设备留下陈旧节点，无法从名字区分。这里只负责
    列出与排序：**判断只能用 USB 设备树或有数据**，不能靠名字。
    """
    found: list[tuple[str, bool]] = []
    for pattern in PORT_GLOBS:
        for path in sorted(glob.glob(pattern)):
            found.append((path, True))
    override = os.environ.get("CORES3_PORT")
    if override and os.path.exists(override):
        found.insert(0, (override, True))
    # 去重，保序
    seen: set[str] = set()
    unique: list[tuple[str, bool]] = []
    for path, live in found:
        if path not in seen:
            seen.add(path)
            unique.append((path, live))
    return unique


class Source(Protocol):
    """数据源协议。run() 阻塞直到 stop() 被调用。"""

    name: str
    detail: str

    def run(self, state: HostState, stop: threading.Event) -> None: ...


class SerialSource:
    """真机串口。

    三层失败都必须**快速、明确**：
      1. 设备不在 USB 上 → 立刻拒绝打开（避免读到陈旧节点却以为一切正常）
      2. 端口被别的进程占用 → 报出占用者（monitor.sh / record_from_device.py 是常见嫌疑）
      3. 端口能开但没有数据 → 10 秒判定，并指出最可能的原因（含 DTR 那一条）
    """

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        no_data_timeout: float = NO_DATA_TIMEOUT_S,
        allow_missing_device: bool = False,
        serial_factory=None,
    ):
        self.requested_port = port
        self.baud = baud
        self.no_data_timeout = no_data_timeout
        self.allow_missing_device = allow_missing_device
        self.name = "serial"
        self.detail = ""
        self._resolved_port: str | None = None
        #: 只给测试用：注入一个假串口，好在不碰真设备的前提下验证重连与瞬态故障处理。
        self._serial_factory = serial_factory
        #: 连接建立/关闭时各回调一次，由上位机用来把句柄交给回控出口。
        self.on_open: Callable[[object], None] | None = None
        self.on_close: Callable[[], None] | None = None

    # ------------------------------------------------------------------ 选择端口

    def resolve_port(self) -> str:
        if self._resolved_port:
            return self._resolved_port

        presence = usb_presence()
        candidates = candidate_ports()
        self.detail = presence.detail

        if self.requested_port:
            if not os.path.exists(self.requested_port):
                raise SourceError(
                    f"指定的串口不存在：{self.requested_port}\n"
                    f"当前候选：{[p for p, _ in candidates] or '（一个都没有）'}\n"
                    "用 CORES3_PORT 覆盖，或先确认设备已插上。"
                )
            if not presence.present and not self.allow_missing_device:
                raise SourceError(
                    f"{presence.detail}\n"
                    f"但 {self.requested_port} 存在——它很可能是设备拔掉后留下的陈旧节点。\n"
                    "先插好设备再启动；确实要用模拟源就加 --mock，"
                    "确知自己在做什么再加 --allow-missing-device。"
                )
            self._resolved_port = self.requested_port
            return self._resolved_port

        if not candidates:
            raise SourceError(
                f"没有找到任何串口（找过 {'、'.join(PORT_GLOBS)}）。\n"
                "  1. 确认 CoreS3 已插上，并且插在**数据**口而不是只供电的口\n"
                "  2. 用 CORES3_PORT=/dev/cu.usbmodemXXXX 显式指定\n"
                "  3. 现在没有设备也可以先看界面：加 --mock"
            )
        if not presence.present and not self.allow_missing_device:
            raise SourceError(
                f"{presence.detail}\n"
                f"当前只剩下这些候选节点：{[p for p, _ in candidates]}\n"
                "它们通常是陈旧节点：能打开、但永远读不到数据。\n"
                "先插好设备；想先看界面就加 --mock。"
            )

        # 有多个候选时优先 usbmodem1101（本机既有约定），否则取编号最小的一个。
        paths = [p for p, _ in candidates]
        preferred = "/dev/cu.usbmodem1101"
        self._resolved_port = preferred if preferred in paths else sorted(paths)[0]
        return self._resolved_port

    # ------------------------------------------------------------------ 运行

    def run(self, state: HostState, stop: threading.Event) -> None:
        # 只有真的要走 pyserial 时才需要这个依赖。注入了 serial_factory（测试用）就不必，
        # 否则单测会被环境里的 pyserial 有无绑架——带上它测的就不再是被测代码了。
        if self._serial_factory is None:
            try:
                import serial  # noqa: PLC0415
            except ImportError as exc:
                raise MissingDependency(
                    "缺少 pyserial，串口模式无法工作。\n"
                    "  1. 用带 pyserial 的解释器（见 README「长窗口漂移采集」一节里那个 uv 缓存路径）\n"
                    "  2. 或 pip install pyserial\n"
                    "  3. 想先看界面：加 --mock"
                ) from exc

        port = self.resolve_port()
        self.detail = f"{port} @ {self.baud}"
        # 端口是在这里才定下来的（可能自动挑选、可能被占用后换端口），
        # 所以要把结果回填给状态层，面板与启动横幅才报得出"到底连了哪一个"。
        state.set_source_detail(self.detail)

        def open_port():
            if self._serial_factory is not None:
                return self._serial_factory(port, self.baud)
            return serial.Serial(port=port, baudrate=self.baud, timeout=0.2)

        try:
            connection = open_port()
        except serial.SerialException as exc:
            raise SourceError(self._explain_open_failure(port, exc)) from exc

        try:
            # 只有内置 USB-Serial/JTAG 才置位 DTR/RTS，理由见 is_native_usb_jtag_port。
            # 这个坑本仓库踩过：tools/monitor.sh 原本对内置 JTAG 设的是 False，
            # 于是端口能开、能写，却一个字节都读不到。
            if is_native_usb_jtag_port(port):
                connection.dtr = ESP32S3_DTR
                connection.rts = ESP32S3_RTS
            if self.on_open is not None:
                self.on_open(connection)
            state.set_connection(True, f"{port} @ {self.baud}")
            state.set_source_detail(self.detail)

            first_data_at: float | None = None
            opened_at = time.monotonic()

            def chunks() -> Iterable[bytes]:
                nonlocal first_data_at
                while not stop.is_set():
                    # 只读"现在就有"的字节，而不是 request 4096 个。
                    #
                    # 这一行是修一个实测了 200 ms 的偏差：pyserial 的 read(size) 会在
                    # 一个**一次性**的超时预算里等到攒满 size 才返回（成功后不重置预算）。
                    # 设备每 150 ms 只产出约 180 字节，4096 字节要 23 帧才凑得满，所以
                    # 每次都必然等满 timeout=0.2 s 才返回，把攒下的 1–2 行一次性交给
                    # 状态归约。表现就是"主机侧逐行到达间隔 p50 = 202 ms"（且 min≈0.02 ms
                    # 是同一批里的第二行），而设备自报节拍是 150.04 ms、丢帧为 0。
                    # 副作用不只是数字难看：面板最多滞后约 208 ms。
                    #
                    # in_waiting 为 0 时读 1 字节：有数据立刻返回，没数据按 timeout 返回，
                    # 于是读取循环的节奏交给设备，而不是交给超时预算。
                    pending_bytes = connection.in_waiting
                    raw = connection.read(pending_bytes if pending_bytes > 0 else 1)
                    if raw:
                        if first_data_at is None:
                            first_data_at = time.monotonic()
                        yield raw
                        continue
                    # 读超时（0.2 s）会回到这里。判定只看"开机以来有没有过数据"，
                    # 所以录音时节拍从 150 ms 放宽到 250 ms 不会误触发这一条。
                    if first_data_at is None and time.monotonic() - opened_at > self.no_data_timeout:
                        raise SourceError(self._explain_silence(port))

            # 读取循环的健壮性是有代价换来的：这个上位机本来会因**一次**瞬时串口错误
            # 整体退出，而"一次"在现场的真实来源是别的进程抢串口（monitor.sh、
            # record_from_device.py 都会开同一个口）。一个跑了几十分钟的会话不该被
            # 一次扰动终结，所以这里改成重连，并且把重连次数与错误次数都报到面板上——
            # 静默重连和不重连一样糟，因为看不出来数据中间断过。
            while not stop.is_set():
                try:
                    for line in iter_json_objects(chunks()):
                        if stop.is_set():
                            break
                        state.feed_line(line)
                except SourceError:
                    state.set_connection(False, "串口读取中止")
                    raise
                except Exception as exc:  # noqa: BLE001 - 串口层的任何异常都按瞬态处理
                    state.note_read_error(str(exc))
                    state.set_connection(False, f"读取中断，正在重连（{exc}）")
                    try:
                        connection.close()
                    except Exception:  # noqa: BLE001
                        pass
                    if self.on_close is not None:
                        self.on_close()
                    if stop.wait(1.0):
                        break
                    try:
                        connection = open_port()
                    except Exception as open_exc:  # noqa: BLE001
                        state.note(f"重连失败：{open_exc}（1 秒后重试）")
                        if stop.wait(1.0):
                            break
                        continue
                    if is_native_usb_jtag_port(port):
                        connection.dtr = ESP32S3_DTR
                        connection.rts = ESP32S3_RTS
                    if self.on_open is not None:
                        self.on_open(connection)
                    state.note_reconnect(f"{port} @ {self.baud}")
                    state.set_connection(True, f"已重连 {port}")
                    first_data_at = None
                    opened_at = time.monotonic()
        finally:
            if self.on_close is not None:
                self.on_close()
            state.set_connection(False, "串口已关闭")
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该盖住真正的错误
                pass

    # ------------------------------------------------------------------ 报错措辞

    def _explain_open_failure(self, port: str, exc: Exception) -> str:
        text = str(exc)
        if "Operation not permitted" in text:
            return (
                f"打开 {port} 被拒绝（Operation not permitted）。\n"
                "通常是运行本程序的沙箱/权限限制。把上位机放在普通终端里跑一次即可确认。"
            )
        if "Resource busy" in text or "Errno 16" in text:
            return (
                f"{port} 已被占用（Resource busy）。\n"
                "同一时刻只能有一个进程开这个串口。先退掉其中一个：\n"
                "  · tools/monitor.sh\n"
                "  · tools/record_from_device.py\n"
                "  · 另一个上位机实例\n"
                "查占用者：lsof " + port
            )
        return f"打开 {port} 失败：{text}"

    def _explain_silence(self, port: str) -> str:
        return (
            f"{port} 打开成功，但 {self.no_data_timeout:.0f} 秒内一个字节都没有。\n"
            "这个端口很可能对应一个**已经不存在的设备**——项目文档记过这一条：\n"
            "  「/dev/cu.usbmodem* 节点在设备拔掉后仍然存在。端口能打开、ls 能看到，\n"
            "    但读不到任何字节。不要用节点在不在判断设备在不在。」\n"
            "按可能性排序的处置：\n"
            "  1. 确认 CoreS3 插的是**数据**口，并且没有被 USB 转接器/只供电的 hub 拦掉\n"
            "  2. 换一个端口：ls /dev/cu.usb*（或 CORES3_PORT=... 覆盖）\n"
            "  3. 设备没在跑：sh tools/monitor.sh 看有没有 ESP-ROM 启动日志，没有就重刷固件\n"
            "  4. 现在没有设备，只想看界面：加 --mock"
        )


class ReplaySource:
    """回放一份历史日志。

    用途很具体：**把一次真有设备在场的会话搬回桌面复核**。没有设备时想验证界面、
    想复现某个异常现场，都靠它。默认按日志里的 timestamp_ms 原速重放，`speed` 可加速。
    """

    def __init__(self, path: str | Path, speed: float = 1.0, loop: bool = False):
        self.path = Path(path)
        self.speed = max(0.0, speed)
        self.loop = loop
        self.name = "replay"
        self.detail = f"{self.path}（×{self.speed:g}）"

    def run(self, state: HostState, stop: threading.Event) -> None:
        if not self.path.exists():
            raise SourceError(f"回放文件不存在：{self.path}")
        state.set_source_detail(self.detail)
        state.set_connection(True, f"回放 {self.path.name}（不是实时设备）")
        try:
            while not stop.is_set():
                self._replay_once(state, stop)
                if not self.loop:
                    break
        finally:
            state.set_connection(False, "回放结束")

    def _replay_once(self, state: HostState, stop: threading.Event) -> None:
        last_timestamp: int | None = None
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if stop.is_set():
                    return
                stamp = _command_timestamp(line)
                if stamp is not None and last_timestamp is not None and self.speed > 0:
                    delay = max(0.0, (stamp - last_timestamp) / 1000.0 / self.speed)
                    if delay > 0 and stop.wait(min(delay, 2.0)):
                        return
                if stamp is not None:
                    last_timestamp = stamp
                state.feed_line(line)


class MockSource:
    """模拟源。

    存在的理由不是"假装有设备"，而是两件事：
      · 没有设备时，界面、状态归约、丢帧检测、记录链路仍然可以被完整跑通并测试；
      · 演示前可以先用它确认上位机自己没问题，再去查设备。
    """

    def __init__(self, seconds: float = 0.0, speed: float = 1.0):
        self.seconds = seconds
        self.speed = max(0.05, speed)
        self.name = "mock"
        self.detail = "模拟源：按固件行为生成帧，不代表任何真实设备"

    def run(self, state: HostState, stop: threading.Event) -> None:
        state.set_source_detail(self.detail)
        state.set_connection(True, self.detail)
        device = MockDevice()
        started = time.monotonic()
        next_at = started
        try:
            while not stop.is_set():
                if self.seconds and time.monotonic() - started >= self.seconds:
                    break
                for line in device.advance(time.monotonic() - started):
                    state.feed_line(line)
                next_at += 0.02 / self.speed
                sleep = next_at - time.monotonic()
                if sleep > 0:
                    if stop.wait(min(sleep, 0.5)):
                        break
                else:
                    next_at = time.monotonic()
        finally:
            state.set_connection(False, "模拟源结束")


def _command_timestamp(line: str) -> int | None:
    """取出 earbud_command 行的 timestamp_ms，用于原速回放。"""
    if '"timestamp_ms"' not in line:
        return None
    import json  # 局部导入：只有回放才需要

    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "earbud_command":
        return None
    value = payload.get("timestamp_ms")
    return value if isinstance(value, int) else None


def build_source(args) -> Source:
    """按命令行参数挑数据源。"""
    if getattr(args, "mock", False):
        return MockSource(seconds=getattr(args, "mock_seconds", 0.0), speed=getattr(args, "speed", 1.0))
    if getattr(args, "replay", None):
        return ReplaySource(args.replay, speed=getattr(args, "speed", 1.0), loop=getattr(args, "loop", False))
    return SerialSource(
        port=getattr(args, "port", None),
        baud=getattr(args, "baud", 115200),
        no_data_timeout=getattr(args, "no_data_timeout", NO_DATA_TIMEOUT_S),
        allow_missing_device=getattr(args, "allow_missing_device", False),
    )
