"""上位机的主机端：HTTP + SSE + 设备回控。

为什么是一个本地网页，而不是一个原生窗口：本项目的既有工程（`ows-earbud-demo`）已经
证明这条路在这台机器上是通的——面板走浏览器、状态走本机端口、`GET /events` 推全帧。
沿用同一套做法有三个具体好处：

1. **不引入依赖。** 这台机器上没有 Xcode，只有 Command Line Tools；Python 侧也只用标准
   库（`http.server` + `threading`）。上位机不该成为"演示前还要装东西"的那个环节。
2. **采全帧，而不是轮询。** 本项目 README 记过一条教训：用 `curl` 轮询读姿态，采到的
   全是过程的边角，得出了错误结论。因此这里推的是**每一帧**（100 ms，约 10 Hz），
   而不是让界面自己去拉。
3. **同一份状态既给人看，也给机器读。** `/api/snapshot` 返回的就是界面渲染用的那个字典，
   所以"界面上显示什么"和"日志里记什么"不会各说一套。

回控只有一条通路，而且**必须**是白名单：设备侧只实现了 `rec start [秒]` / `rec stop`
（main/app_main.cpp: poll_console_command）。上位机不做通用串口终端——那等于把固件的
命令行暴露给一个网页。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .player import PlayerError
from .state import HostState
from .systemeq import SystemEqError

WEB_ROOT = Path(__file__).resolve().parent / "web"
INDEX_FILE = WEB_ROOT / "index.html"

#: SSE 推送的最小间隔。设备每 100 ms 来一帧，界面按帧推即可；加一个下限是为了
#: 回放加速时不要把浏览器打爆。
MIN_PUSH_INTERVAL_S = 0.05
#: 允许写入设备的最大录音秒数，避免把串口长时间占成录音流。
MAX_RECORD_SECONDS = 3600
#: 发完命令等设备回应的时间。设备在一个节拍（100 ms）内就会输出 capture 事件，2 秒足够；
#: 再长只会让界面卡住，而"设备不回应"本身就是要报出来的结论。
CONTROL_ACK_TIMEOUT_S = 2.0


class CommandError(Exception):
    """回控请求不合法。消息会原样回给界面。"""


def build_command(raw: str) -> str:
    """把界面上的请求翻译成设备唯一能懂的命令行。

    只接受两种形状，其余一律拒绝：
        rec start          → 一直录，直到 rec stop
        rec start <秒数>   → 录指定秒数（1…3600）

    注意这里**不做**"顺手转发"，因为设备侧的命令解析是 `strncmp("rec start", 9)` 加
    `sscanf`，任何别的输入都只会被丢弃——但上位机不该让用户以为它发出去了什么。
    """
    text = (raw or "").strip()
    if not text:
        raise CommandError("命令为空")
    lowered = text.lower()
    if lowered == "rec stop":
        return "rec stop"
    if lowered.startswith("rec start"):
        rest = text[len("rec start") :].strip()
        if not rest:
            return "rec start"
        try:
            seconds = int(rest)
        except ValueError as exc:
            raise CommandError(f"录音秒数必须是整数，收到 {rest!r}") from exc
        if not 1 <= seconds <= MAX_RECORD_SECONDS:
            raise CommandError(f"录音秒数需在 1…{MAX_RECORD_SECONDS} 之间，收到 {seconds}")
        return f"rec start {seconds}"
    raise CommandError("只支持 rec start [秒数] 与 rec stop；上位机不是通用串口终端")


class Broadcaster:
    """把新快照推给所有 SSE 连接。

    每个订阅者一个容量为 1 的"最新值"槽，而不是一个队列：界面要的是**当前状态**，
    不是历史。慢的消费者只会丢掉中间帧，不会把内存撑大，也不会拖慢数据源线程。
    """

    def __init__(self, state: HostState, min_interval: float = MIN_PUSH_INTERVAL_S):
        self.state = state
        self.min_interval = min_interval
        self._condition = threading.Condition()
        self._slots: dict[int, dict[str, Any] | None] = {}
        self._next_id = 0
        self._last_push = 0.0
        self._version = 0

    def publish(self) -> None:
        with self._condition:
            now = time.monotonic()
            if now - self._last_push < self.min_interval:
                return
            self._last_push = now
            self._version += 1
            for key in self._slots:
                self._slots[key] = None  # 置脏，由消费者自己去取最新快照
            self._condition.notify_all()

    def subscribe(self):
        with self._condition:
            key = self._next_id
            self._next_id += 1
            self._slots[key] = None
        return key

    def unsubscribe(self, key: int) -> None:
        with self._condition:
            self._slots.pop(key, None)

    def wait_for_change(self, key: int, timeout: float = 1.0) -> dict[str, Any] | None:
        """等下一次变化，返回最新快照。

        超时返回一个心跳快照：**没有帧也要推**，否则界面无法区分"设备停了"和"浏览器
        和主机之间断了"——这两种情况在现场的处置完全不同。
        """
        with self._condition:
            self._condition.wait_for(lambda: self._slots.get(key) is not None, timeout=timeout)
            self._slots[key] = None
        return self.state.snapshot()


class HostHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state: HostState, broadcaster: Broadcaster, writer=None,
                 releaser=None, player=None, systemeq=None, on_stop=None, exit_fn=None):
        super().__init__(address, handler)
        self.state = state
        self.broadcaster = broadcaster
        self.writer = writer
        #: 可调用对象：释放串口（供 /api/stop 用）。
        self.releaser = releaser
        #: Mac 播放与执行端（tools/host/player.py）。为 None 表示本次没启用播放器，
        #: 面板上的播放控件要明确禁用而不是假装点过了。
        self.player = player
        #: 第三方播放器 system EQ 执行端（tools/host/systemeq.py）。为 None 表示没启用。
        self.systemeq = systemeq
        #: /api/stop 的收尾回调（正常退出路径也复用同一个幂等 cleanup）。None = 没有。
        #: 必须在 os._exit 之前跑，否则 AirPods 聆听模式、子进程、日志都不会被收掉。
        self.on_stop = on_stop
        #: 真正结束进程的函数。注入点只给测试用，生产走 os._exit(0)。
        self.exit_fn = exit_fn or os._exit
        self.started_at = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    server_version = "CoreS3Host/1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 工具

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 覆盖基类
        # 默认实现会往 stderr 打每一行请求；上位机自己已经在面板上显示连接状态了，
        # 再把 HTTP 日志混进终端只会淹没设备日志。
        return

    @property
    def state(self) -> HostState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def releaser(self):
        return getattr(self.server, "releaser", None)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/snapshot":
            self._send_json(self.state.snapshot())
        elif path == "/api/health":
            snapshot = self.state.snapshot()
            self._send_json(
                {
                    "ok": True,
                    "source": snapshot["source"],
                    "connected": snapshot["connected"],
                    "stale": snapshot["stale"],
                    "commands": snapshot["stats"]["commands"],
                    "uptime_s": snapshot["uptime_s"],
                }
            )
        elif path == "/api/stop":
            # 释放串口并让进程退出。存在的理由很具体：tools/record_from_device.py 与刷机
            # 都需要独占这个端口，而"上位机开着的时候录音工具打不开串口"是个卡人的死结。
            # 面板上点一下就能让出来，比让使用者去找进程号好。
            released = False
            releaser = self.releaser
            if releaser is not None:
                try:
                    released = bool(releaser())
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
            self._send_json({"ok": True, "released_port": released, "note": "进程即将退出"})
            # on_stop/exit_fn 由 server 持有：生产是幂等 cleanup + os._exit，
            # 测试注入假实现，不需要真的结束进程。
            on_stop = getattr(self.server, "on_stop", None)
            exit_fn = getattr(self.server, "exit_fn", os._exit)
            threading.Thread(target=self._delayed_exit, args=(on_stop, exit_fn), daemon=True).start()
        elif path == "/events":
            self._serve_events()
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

    @staticmethod
    def _delayed_exit(on_stop=None, exit_fn=None, delay: float = 0.3) -> None:
        # 先让响应发出去，再退出——否则浏览器只会看到连接被重置。
        time.sleep(delay)
        # 收尾（切回 AirPods 聆听模式 / 停子进程 / 关日志）必须在 os._exit 之前跑：
        # os._exit 不执行 finally/atexit，跳过它就会把用户留在通透模式里。
        if on_stop is not None:
            try:
                on_stop()
            except Exception as exc:  # noqa: BLE001 - 收尾失败不能阻止进程退出
                print(f"/api/stop 收尾失败：{exc}", file=sys.stderr)
        (exit_fn or os._exit)(0)

    def _serve_index(self) -> None:
        if not INDEX_FILE.exists():
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"缺少界面文件 {INDEX_FILE}".encode("utf-8"),
                "text/plain; charset=utf-8",
            )
            return
        self._send(HTTPStatus.OK, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        broadcaster: Broadcaster = self.server.broadcaster  # type: ignore[attr-defined]
        key = broadcaster.subscribe()
        try:
            # 先推一帧，让界面立刻有内容，而不是等设备下一帧。
            self._write_event(self.state.snapshot())
            while True:
                snapshot = broadcaster.wait_for_change(key)
                if snapshot is None:
                    break
                self._write_event(snapshot)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器刷新/关闭：正常现象，不是错误
        finally:
            broadcaster.unsubscribe(key)

    def _write_event(self, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------------ POST

    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(payload, dict):
            self._send_json({"ok": False, "error": "请求体必须是 JSON 对象"}, HTTPStatus.BAD_REQUEST)
            return None
        return payload

    def _handle_execution(self) -> None:
        """演示暂停/继续（2026-10-26）。

        与播放回控同一条原则：**只改一个明确的状态位**，由主循环决定执行端怎么复位。
        暂停不碰采集/AI/设备；只停止把 S3 的决策下发到音频。
        """
        payload = self._read_json_body()
        if payload is None:
            return
        paused = payload.get("paused")
        if not isinstance(paused, bool):
            self._send_json(
                {"ok": False, "error": "缺少 paused 布尔字段"}, HTTPStatus.BAD_REQUEST
            )
            return
        setter = getattr(self.state, "set_execution_paused", None)
        if setter is None:  # pragma: no cover - 旧 HostState 兼容
            self._send_json(
                {"ok": False, "error": "当前状态对象不支持暂停"}, HTTPStatus.CONFLICT
            )
            return
        setter(paused)
        self._send_json({"ok": True, "paused": paused})

    def _handle_player(self) -> None:
        """Mac 播放与执行端（player / systemeq）的回控。与设备回控同一条原则：
        **白名单，不是通用终端**。

        面板上只有播放/暂停、切曲、音量、处方深度这几个动作，所以这里只放行这几个。
        执行端没启用或没构建出来时明确拒绝并带回原因——按钮"点过了"但什么都没发生，
        是这块面板最不该有的谎。
        """
        # 必须先读 body 再判断执行端：HTTP/1.1 keep-alive 下，不消费请求体，
        # 残留字节会和下一条请求拼成畸形请求行（表现为 501 Unsupported method）。
        payload = self._read_json_body()
        if payload is None:
            return
        command = payload.pop("cmd", None)
        if not isinstance(command, str):
            self._send_json({"ok": False, "error": "缺少 cmd 字段"}, HTTPStatus.BAD_REQUEST)
            return

        player = getattr(self.server, "player", None)
        systemeq = getattr(self.server, "systemeq", None)

        if command == "depth":
            try:
                if systemeq is not None:
                    result = systemeq.set_depth(float(payload.get("depth", systemeq.depth)))
                elif player is not None:
                    result = player.set_depth(float(payload.get("depth", player.depth)))
                else:
                    self._send_json(
                        {"ok": False, "error": "本次运行没有启用任何执行端"},
                        HTTPStatus.CONFLICT,
                    )
                    return
            except (SystemEqError, PlayerError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except (TypeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": f"参数不合法：{exc}"}, HTTPStatus.BAD_REQUEST
                )
                return
            snapshot: dict[str, Any] = {}
            if player is not None:
                snapshot["player"] = player.snapshot()
            if systemeq is not None:
                snapshot["systemeq"] = systemeq.snapshot()
            self._send_json({"ok": True, "result": result, **snapshot})
            return

        if command == "mode":
            # EQ 档位（basic=网友共识克制版 / aggressive=×2 明显版）只属于 systemeq。
            if systemeq is None:
                self._send_json(
                    {"ok": False, "error": "EQ 档位只属于 systemeq（本次运行没有 --system-eq-app）"},
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                result = systemeq.command("mode", value=str(payload.get("value", "")))
            except SystemEqError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "result": result, "systemeq": systemeq.snapshot()})
            return

        if command == "bypass":
            # "关掉所有 EQ"：执行端直通。有 systemeq 优先（QQ 音乐链路），否则走 player。
            try:
                if systemeq is not None:
                    result = systemeq.command("bypass", on=bool(payload.get("on")))
                elif player is not None:
                    result = player.command("bypass", on=bool(payload.get("on")))
                else:
                    self._send_json(
                        {"ok": False, "error": "本次运行没有启用任何执行端"},
                        HTTPStatus.CONFLICT,
                    )
                    return
            except (SystemEqError, PlayerError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            snapshot_data: dict[str, Any] = {}
            if player is not None:
                snapshot_data["player"] = player.snapshot()
            if systemeq is not None:
                snapshot_data["systemeq"] = systemeq.snapshot()
            self._send_json({"ok": True, "result": result, **snapshot_data})
            return

        if player is None:
            self._send_json(
                {"ok": False, "error": "本次运行没有启用 Mac 播放器（用 --no-player 起的）"},
                HTTPStatus.CONFLICT,
            )
            return
        try:
            result = player.command(command, **payload)
        except PlayerError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (TypeError, ValueError) as exc:
            self._send_json({"ok": False, "error": f"参数不合法：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "result": result, "player": player.snapshot()})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/execution":
            self._handle_execution()
            return
        if path == "/api/player":
            self._handle_player()
            return
        if path != "/api/command":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("command"), str):
            self._send_json({"ok": False, "error": "缺少 command 字段"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            command = build_command(payload["command"])
        except CommandError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        writer = self.server.writer  # type: ignore[attr-defined]
        if writer is None:
            self._send_json(
                {
                    "ok": False,
                    "error": "当前数据源不可回控（模拟源或回放都没有设备可写）",
                },
                HTTPStatus.CONFLICT,
            )
            return

        # 回执判据：只认**发送之后新产生的 capture 事件**。capture_state 是粘性的
        # （一次录音结束后会一直停在 stopped/finished），拿它比对会把"设备没有回应"
        # 误报成"设备已回应"——那正是这里最不该撒的谎。
        before_seq = self.state.snapshot()["capture"]["event_seq"]
        self.state.note_control_sent(command)
        try:
            writer(command.encode("ascii") + b"\r\n")
        except Exception as exc:  # noqa: BLE001 - 串口写失败的原因要原样带回界面
            self._send_json({"ok": False, "error": f"写入设备失败：{exc}"}, HTTPStatus.BAD_GATEWAY)
            return

        # 写成功 != 设备照做。这里等一个**发送之后**才出现的 capture 事件，
        # 拿不到就如实报降级，而不是回一个 ok:true 让按钮看起来点过了。
        acknowledged = False
        deadline = time.monotonic() + CONTROL_ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            current_seq = self.state.snapshot()["capture"]["event_seq"]
            if current_seq > before_seq:
                acknowledged = True
                break
            time.sleep(0.1)

        self.state.note_control_result(
            acknowledged,
            f"{CONTROL_ACK_TIMEOUT_S:.1f} 秒内没有 capture 事件",
        )
        if not acknowledged:
            self._send_json(
                {
                    "ok": False,
                    "command": command,
                    "acknowledged": False,
                    "warning": (
                        f"命令 {command} 已写入串口，但 {CONTROL_ACK_TIMEOUT_S:.1f} 秒内"
                        "设备没有任何回应：它没有进入录音态。"
                        "这说明设备侧的串口输入没有被消费（本机实测 write() 成功但设备无反应，"
                        "与控制台输入未接到 USB-Serial/JTAG 的症状一致），"
                        "不是按钮没生效。"
                    ),
                },
                HTTPStatus.BAD_GATEWAY,
            )
            return

        self._send_json({"ok": True, "command": command, "acknowledged": True})


def make_server(
    state: HostState,
    broadcaster: Broadcaster,
    port: int,
    host: str = "127.0.0.1",
    writer=None,
    releaser=None,
    player=None,
    systemeq=None,
    on_stop=None,
    exit_fn=None,
) -> HostHTTPServer:
    return HostHTTPServer(
        (host, port), Handler, state, broadcaster, writer, releaser, player, systemeq,
        on_stop, exit_fn
    )


def pick_port(preferred: int, host: str = "127.0.0.1") -> int:
    """优先用指定端口；被占用时向后找 10 个。

    端口被占用时**必须换一个继续跑**，而不是退出：演示现场最常见的占用原因是上一次
    没退干净的上位机，此时最该做的是把新的这个跑起来，并如实报出用的是哪个端口。
    """
    for candidate in range(preferred, preferred + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError(f"{preferred}…{preferred + 9} 都被占用，换一个 --web-port")
