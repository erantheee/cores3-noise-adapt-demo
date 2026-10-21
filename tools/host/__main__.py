"""上位机：把 CoreS3 的控制帧变成一个看得见、也能回控的面板。

    python3 -m host                 # 串口真机（自动找端口）
    python3 -m host --mock          # 没有设备也能先把界面跑起来
    python3 -m host --replay 记录/x.log --speed 4

启动后打开 http://127.0.0.1:8787/ 看面板。

退出码：0 正常停止 · 1 运行期错误（含设备不在场）· 2 参数错误 · 3 缺少依赖
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

from .ambient import AmbientService, RelayAmbient
from .audio_relay import (
    DEFAULT_DEVICE,
    DEFAULT_TARGET_MS,
    PLAYER_FFPLAY,
    PLAYER_SINK,
    AudioRelay,
    RelayError,
)
from .mac_audio import EXPECTED_TARGET, default_output, route_command
from .player import DEFAULT_VOLUME_PERCENT, PlayerService
from .server import Broadcaster, make_server, pick_port
from .voiceprint import VoiceprintService
from .sources import (
    MissingDependency,
    MockSource,
    ReplaySource,
    SerialSource,
    SourceError,
    build_source,
)
from .state import HostState

DEFAULT_WEB_PORT = 8787


def default_airpods_cli() -> str:
    """默认的兄弟工程 CLI 路径：../airpods-control/bin/airpods。

    不把它复制/依赖成包：那是独立的 macOS 工具（AX 控制中心），有自己的一套实测与授权。
    找不到就如实报"不可用"，不是崩溃。
    """
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root.parent / "airpods-control" / "bin" / "airpods")


def make_cleanup(steps: Sequence[tuple[str, Callable[[], None]]]) -> Callable[[], None]:
    """把多个收尾动作包装成**幂等、异常隔离**的一次性清理函数。

    两条退出路径共用它：/api/stop（HTTP 线程的 on_stop）与正常退出（主线程）。
    两个约束都不是洁癖：
      * 幂等：/api/stop 之后进程还会被 os._exit 结束，cleanup 可能被两条路径各调一次；
      * 异常隔离：单个服务停不下来不能阻止"把 AirPods 切回原聆听模式"这类更要紧的动作。

    返回的函数带 `.errors`（字符串列表，供退出报告）与 `.done()`（是否已经执行过）。
    """
    lock = threading.Lock()
    state = {"done": False}
    errors: list[str] = []

    def cleanup() -> None:
        with lock:
            if state["done"]:
                return
            state["done"] = True
        for label, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - 收尾动作之间必须互相隔离
                errors.append(f"{label}: {exc}")

    cleanup.errors = errors  # type: ignore[attr-defined]
    cleanup.done = lambda: state["done"]  # type: ignore[attr-defined]
    return cleanup


class DeviceWriter:
    """设备回控的唯一出口。

    串口句柄由**数据源线程**持有（只有它知道连接是否还活着），HTTP 线程只通过这里写。
    用一把锁把"取句柄"和"写"绑在一起，是为了避免一个正在被关闭的连接被另一个线程写：
    那种错误在现场表现为"点了按钮没反应"或串口报错，很难查。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection = None

    def attach(self, connection) -> None:
        with self._lock:
            self._connection = connection

    def detach(self) -> None:
        with self._lock:
            self._connection = None

    @property
    def available(self) -> bool:
        with self._lock:
            return self._connection is not None

    def __call__(self, data: bytes) -> None:
        with self._lock:
            if self._connection is None:
                raise RuntimeError("串口未打开")
            self._connection.write(data)
            self._connection.flush()

    def send_voiceprint(self, verdict: str, score: float) -> None:
        """把 WavLM 的 self/other 判定回传给 S3（纯文本控制行）。"""
        self(f"voiceprint {verdict} {float(score):.2f}\n".encode("ascii"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m host",
        description="CoreS3 上位机：实时显示 EarbudCommand 帧，并可回控设备录音。",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--port", help="串口设备（默认自动找 /dev/cu.usbmodem*）")
    source.add_argument("--mock", action="store_true", help="用模拟源（不需要设备）")
    source.add_argument("--replay", help="回放一份历史串口日志")
    parser.add_argument("--baud", type=int, default=115200, help="波特率，默认 115200")
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=f"面板端口，默认 {DEFAULT_WEB_PORT}",
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--speed", type=float, default=1.0, help="回放/模拟的速度倍率")
    parser.add_argument("--loop", action="store_true", help="回放循环")
    parser.add_argument(
        "--mock-seconds", type=float, default=0.0, help="模拟源运行多少秒后停止（0=一直跑）"
    )
    parser.add_argument(
        "--no-data-timeout",
        type=float,
        default=10.0,
        help="端口打开后多久没有数据就判定设备不在（秒）",
    )
    parser.add_argument(
        "--allow-missing-device",
        action="store_true",
        help="USB 设备树里没有 Espressif 设备时也照样打开指定端口（默认拒绝，避免读到陈旧节点）",
    )
    parser.add_argument("--quiet", action="store_true", help="不打印启动说明")
    parser.add_argument(
        "--no-player",
        action="store_true",
        help="不起 Mac 播放与执行端（tools/player）；默认会起，面板上据此出现播放控件",
    )
    parser.add_argument(
        "--player-volume",
        type=float,
        default=DEFAULT_VOLUME_PERCENT,
        help=f"播放增益百分比（100%% 为原始电平），默认 {DEFAULT_VOLUME_PERCENT:.0f}，可在面板上调节",
    )
    parser.add_argument(
        "--relay-audio",
        action="store_true",
        help="把 CoreS3 麦克风实时中继到 Mac 当前默认输出（AirPods）",
    )
    parser.add_argument(
        "--relay-player",
        choices=(PLAYER_SINK, PLAYER_FFPLAY),
        default=PLAYER_SINK,
        help=(
            "中继播放端：sink（默认，tools/relay/relaysink，报真实排队深度并按上限丢弃）"
            "或 ffplay（旧路径，延迟不可观测，只为 A/B 对比保留）"
        ),
    )
    parser.add_argument(
        "--relay-device",
        default=DEFAULT_DEVICE,
        help=(
            f"中继要钉住的输出设备名（子串匹配），默认 {DEFAULT_DEVICE or '跟随系统默认输出'}。"
            "只钉这条流的输出，不改系统默认输出；设备不在时会如实报警"
        ),
    )
    parser.add_argument(
        "--relay-target-ms",
        type=float,
        default=DEFAULT_TARGET_MS,
        help=f"播放端允许的最大排队深度（毫秒），默认 {DEFAULT_TARGET_MS:g}",
    )
    parser.add_argument(
        "--ambient-executor",
        choices=("off", "relay", "airpods"),
        default="off",
        help=(
            "环境声执行端。off=只显示 S3 的 ambient_open 意图；"
            "relay=当前 demo 路径：S3 麦克风 → Mac → 耳机中继，S3 说开就放行音频"
            "（固有时延约 211–231 ms，先把预判与开关跑通）；"
            "airpods=后续拓展路径：用兄弟工程 airpods-control 把 AirPods 切到通透模式"
            "（需要辅助功能权限，会短暂开合控制中心声音面板）"
        ),
    )
    parser.add_argument(
        "--airpods-cli",
        default=default_airpods_cli(),
        help="airpods-control 的 CLI 路径；默认取兄弟工程 ../airpods-control/bin/airpods",
    )
    parser.add_argument(
        "--voiceprint",
        action="store_true",
        help=(
            "实验性 self-veto 声纹门控：self → 设备不压/不抬/不开环境声；"
            "other → 按 D16 预判逻辑。判定回传 S3，默认关闭；"
            "需要仓库 .venv 里的 torch/transformers。"
        ),
    )
    parser.add_argument(
        "--voiceprint-enroll-dir",
        default=None,
        help="佩戴者声纹注册录音目录（默认 记录/声纹/self-device）",
    )
    parser.add_argument(
        "--voiceprint-threshold",
        type=float,
        default=0.80,
        help="self cosine 判定阈值（默认 0.80；结果参与双方轮次门控）",
    )
    parser.add_argument(
        "--voiceprint-python",
        default=None,
        help="跑 voiceprint_worker.py 的解释器（默认仓库 .venv/bin/python）",
    )
    parser.add_argument(
        "--ambient-mode",
        choices=("transparency", "adaptive", "anc"),
        default="transparency",
        help="（--ambient-executor airpods）开放环境声时切到的聆听模式，默认 transparency",
    )
    parser.add_argument(
        "--frames-jsonl",
        default="",
        help=(
            "把每帧解析结果追加写成 JSONL（对话预判训练集用）。"
            "包含判据遥测、环境谱、决策与环境声意图；标签由人另外标（见 docs/conversation-prediction.md）"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = build_source(args)
    # relay 执行端需要中继基础设施；--relay-audio 只表示"开机就常开中继"。
    relay_needed = args.relay_audio or args.ambient_executor == "relay" or args.voiceprint
    relay = (
        AudioRelay(
            player=args.relay_player,
            device=args.relay_device,
            target_ms=args.relay_target_ms,
        )
        if relay_needed
        else None
    )
    if relay is not None:
        try:
            relay.start()
        except RelayError as exc:
            print(f"无法启动音频中继：{exc}", file=sys.stderr)
            return 3
    # 环境声执行端必须在 HostState 之前定下来：on_mic_chunk 要按"门控"还是"直通"接。
    # relay 执行端走门控（S3 说开才放行），--relay-audio 则是常开中继（调试/对照）。
    relay_ambient = RelayAmbient(relay) if args.ambient_executor == "relay" and relay else None
    if args.ambient_executor == "relay" and relay is None:
        print("无法启用中继执行端：中继没有创建", file=sys.stderr)
        return 3
    if relay_ambient is not None:
        # HostState 传的是 MicChunk（含 pcm/rate），RelayAmbient.feed 要两个参数。
        # 2026-10-19 实测这个签名错会让每个 mic_chunk 抛 TypeError，串口每秒重连一次，
        # 中继环境声音频根本进不来——"中继一直好好的"其实是这条回调从没跑通过。
        base_feed = lambda chunk: relay_ambient.feed(chunk.pcm, chunk.rate)  # noqa: E731
    elif relay is not None and args.relay_audio:
        base_feed = lambda chunk: relay.feed(chunk.pcm, chunk.rate)  # noqa: E731
    else:
        base_feed = None

    voiceprint = None
    if args.voiceprint:
        voiceprint_kwargs = {"threshold": args.voiceprint_threshold}
        if args.voiceprint_python:
            voiceprint_kwargs["python"] = args.voiceprint_python
        if args.voiceprint_enroll_dir:
            voiceprint_kwargs["enroll_dir"] = args.voiceprint_enroll_dir
        voiceprint = VoiceprintService(**voiceprint_kwargs)
        voiceprint.start()

    def on_mic_chunk(chunk):
        if base_feed is not None:
            base_feed(chunk)
        if voiceprint is not None:
            voiceprint.feed(chunk)

    if base_feed is None and voiceprint is None:
        on_mic_chunk = None
    state = HostState(
        source=source.name,
        detail=source.detail,
        on_mic_chunk=on_mic_chunk,
    )
    if relay is not None:
        # 中继的数字要进快照：这条通路上原来最缺的就是"现在到底晚了多少"。
        # 顺序要紧：attach 必须发生在 HostState 建好之后（这里曾经写成之前，结果是
        # 启动就 UnboundLocalError —— 而所有单测都测不到 main()，是启动横幅救的场）。
        state.attach_relay(relay)
    if relay_ambient is not None:
        ambient = relay_ambient
    elif args.ambient_executor == "airpods":
        ambient = AmbientService(args.airpods_cli, open_mode=args.ambient_mode)
    else:
        ambient = None
    if ambient is not None:
        state.attach_ambient(ambient)
    if voiceprint is not None:
        state.attach_voiceprint(voiceprint)
    broadcaster = Broadcaster(state)
    writer = DeviceWriter()

    def send_voiceprint_off() -> None:
        """尽力告诉设备「声纹判定已失效」，避免它沿用上一轮判定。

        串口没打开/已断开就算了：这只是清理动作，不能反过来阻断退出。
        """
        if voiceprint is None:
            return
        try:
            writer.send_voiceprint("off", 0.0)
        except (RuntimeError, OSError):
            pass

    if voiceprint is not None:
        def _send_voiceprint(verdict: str, score: float) -> None:
            try:
                writer.send_voiceprint(verdict, score)
                voiceprint.note_sent()
            except (RuntimeError, OSError):
                # 串口没打开/刚断开：声纹遥测继续，判定不硬塞。
                pass

        voiceprint.on_verdict = _send_voiceprint

    stop = threading.Event()

    def request_stop(signum=None, frame=None) -> None:  # noqa: ANN001, ARG001
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    # 只有串口源可回控。模拟源与回放都没有设备可写，界面会据此禁用按钮。
    can_control = isinstance(source, SerialSource)
    if can_control:
        def _attach_writer(connection) -> None:
            writer.attach(connection)
            # 只有 worker 真的 ready 才发 pending：worker 不可用时发出去，设备会进入
            # "等声纹判定"的状态而永远等不到后续判定（旧实现就是这么把对话门控锁死的）。
            # 现在的固件对 pending 只做重置、判定 3 秒过期，但不在源头发无意义的 pending 更干净。
            if voiceprint is not None and voiceprint.ready:
                writer.send_voiceprint("pending", 0.0)

        def _detach_writer() -> None:
            # 串口断开/退出前尽力告诉设备判定已失效，再把回控句柄摘掉（顺序不能反）。
            send_voiceprint_off()
            writer.detach()

        source.on_open = _attach_writer
        source.on_close = _detach_writer

    def release_port() -> bool:
        """释放串口：把录音工具或刷机需要的独占权让出去。

        只有 SerialSource 有端口可让；源线程会因此退出读取循环并 close()。macOS 上
        "只是停线程"并不释放 fd，所以关闭这一步必须真做（sources.SerialSource 的 finally）。
        """
        if not isinstance(source, SerialSource):
            return False
        source.requested_stop.set()
        # 等它真的关掉，最多 3 秒，然后把结果如实回报给调用者。
        for _ in range(30):
            if getattr(source, "close_connection", None) is None:
                return True
            time.sleep(0.1)
        return False

    web_port = pick_port(args.web_port)

    # Mac 播放与执行端。它不属于"数据源"，而是执行端：S3 决策 → 这里落到音频上。
    # 起不来（没构建、缺曲库）不算致命错误——面板照样跑，但会把原因写在播放卡片里，
    # 而不是让播放按钮看起来能用。
    player_service = None
    if not args.no_player:
        player_service = PlayerService(volume_percent=args.player_volume)
        state.attach_player(player_service)

    # 逐帧日志（--frames-jsonl）稍后才打开，但要把变量先定下来：收尾 cleanup 会引用它。
    frames_log = None

    # 收尾只有一份：/api/stop（on_stop）与正常退出（主线程）共用。
    # 顺序有讲究：**先**把 AirPods 从通透模式切回去（ambient.stop 的唯一入口），
    # 再停播放/声纹/中继，最后关日志；单个服务失败不影响其余动作（make_cleanup 隔离异常）。
    cleanup_steps: list[tuple[str, Callable[[], None]]] = []
    if ambient is not None:
        cleanup_steps.append(("环境声执行端", ambient.stop))
    if player_service is not None:
        cleanup_steps.append(("Mac 播放器", player_service.stop))
    if voiceprint is not None:
        cleanup_steps.append(("声纹 off 回执", send_voiceprint_off))
        cleanup_steps.append(("声纹 worker", voiceprint.stop))
    if relay is not None:
        cleanup_steps.append(("音频中继", relay.stop))
    cleanup_steps.append(
        ("逐帧日志", lambda: frames_log.close() if frames_log is not None else None)
    )
    cleanup = make_cleanup(cleanup_steps)

    httpd = make_server(
        state,
        broadcaster,
        web_port,
        writer=writer if can_control else None,
        releaser=release_port,
        player=player_service,
        on_stop=cleanup,
    )
    url = f"http://127.0.0.1:{web_port}/"

    if not args.quiet:
        print("=" * 66)
        print("CoreS3 上位机")
        print(f"  数据源    {state.source}  {source.detail or ''}")
        print(f"  面板      {url}")
        print("  帧率      设备每 100 ms 一帧；录音时放宽到 250 ms（预期行为）")
        print("  回控      可发 rec start [秒数] / rec stop" if can_control else "  回控      不可用（当前数据源没有设备可写）")
        if relay:
            print(f"  中继      S3 麦克风 → {args.relay_device}（播放端 {relay.mode}，"
                  f"排队上限 {args.relay_target_ms:g} ms）")
            if relay.mode == PLAYER_FFPLAY:
                print("  注意      ffplay 不报告排队深度：这条路的延迟只能靠声学测量，"
                      "面板会显示为「测不到」")
        else:
            print("  中继      未开启（加 --relay-audio 或 --ambient-executor relay）")
        if relay_ambient is not None:
            print(
                "  环境声    S3 ambient_open 意图 → S3 麦克风中继到耳机"
                f"（执行端 s3-relay，设备「{args.relay_device}」，"
                f"排队上限 {args.relay_target_ms:g} ms）"
            )
            print("  注意      中继通路的固有时延约 211–231 ms（蓝牙占 170 ms）；"
                  "这一版只验证预判与开关，不宣称能听清对面")
        elif isinstance(ambient, AmbientService):
            print(
                f"  环境声    S3 ambient_open 意图 → AirPods「{args.ambient_mode}」"
                f"（执行端 {ambient.cli_path}）"
            )
            if not ambient.available:
                print("  注意      找不到 airpods-control CLI：意图会显示，但执行端不可用")
        else:
            print("  环境声    只显示 S3 意图（--ambient-executor relay|airpods 才执行）")
        if isinstance(source, ReplaySource):
            print("  注意      回放只是把历史日志搬到界面上，不代表设备现在的状态")
        if isinstance(source, MockSource):
            print("  注意      模拟源不代表任何真实设备，link 字段一律显示 simulated")
        print("  Ctrl+C 退出")
        print("=" * 66)

    thread_error: list[BaseException] = []

    def run_source() -> None:
        try:
            source.run(state, stop)
        except (MissingDependency, SourceError) as exc:
            thread_error.append(exc)
        except BaseException as exc:  # noqa: BLE001 - 未预期错误也要带回主线程
            thread_error.append(exc)
        finally:
            writer.detach()
            stop.set()
            broadcaster.publish()

    source_thread = threading.Thread(target=run_source, name="source", daemon=True)
    source_thread.start()

    # CoreS3 cannot inspect macOS Bluetooth routing itself. Report the Mac's
    # real default output to its second page whenever that route changes.
    def monitor_mac_route() -> None:
        last_device: str | None | object = object()
        while not stop.wait(5.0):
            device = default_output()
            if device == last_device or not writer.available:
                continue
            try:
                writer(route_command(device))
                last_device = device
                state.log("route", "Mac 音频输出已同步", device or "没有默认输出")
                # 真机踩过：AirPods 掉线后默认输出自动落到 MacBook Air 扬声器，中继照常播放，
                # 面板只记了一行路由变更。用户戴着耳机听不到声音时，会以为是自己这边的问题，
                # 于是往延迟、增益、缓冲的方向查——真正的原因（耳机压根没连）反而没人说。
                # 所以这里必须报警，而不是只记录。
                if relay is not None and device != EXPECTED_TARGET:
                    state.note(
                        f"中继正在播放到「{device or '没有默认输出'}」，不是 {EXPECTED_TARGET}。"
                        "声音不会进到你戴的耳机里。先确认耳机已连上这台 Mac，并在"
                        "「系统设置 → 声音 → 输出」里选中它。"
                    )
            except (OSError, RuntimeError):
                continue

    if can_control:
        threading.Thread(target=monitor_mac_route, name="mac-route", daemon=True).start()

    def start_live_capture() -> None:
        """Start indefinite device capture after the serial console has finished booting."""
        if relay is None:
            return
        deadline = time.monotonic() + 10.0
        while not stop.is_set() and time.monotonic() < deadline:
            if writer.available:
                time.sleep(0.8)
                try:
                    writer(b"rec start\r\n")
                    state.note_control_sent("rec start（音频中继）")
                    return
                except (OSError, RuntimeError):
                    pass
            time.sleep(0.1)
        state.note("音频中继未能启动：10 秒内没有可写的设备连接")

    if can_control and relay is not None:
        threading.Thread(target=start_live_capture, name="relay-start", daemon=True).start()

    serving = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    serving.start()

    if not args.no_browser and not args.quiet:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    # 可选：逐帧落盘，作为对话预判的训练/复盘数据（对齐主机单调时钟，便于和录音对齐）。
    if args.frames_jsonl:
        try:
            frames_log = open(args.frames_jsonl, "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            print(f"无法打开 --frames-jsonl 文件：{exc}", file=sys.stderr)
            return 2
    logged_sequence: int | None = None
    started_monotonic = time.monotonic()

    # 主线程只做一件事：按节拍发布快照，并把最新一帧的决策下发给 Mac 执行端。
    # 没有新帧时也发快照，界面才能区分"设备停了"与"浏览器和主机之间的连接断了"——
    # 这两种情况的处置完全不同。
    while not stop.is_set():
        broadcaster.publish()
        latest = state.latest
        if (
            frames_log is not None
            and latest is not None
            and latest.sequence != logged_sequence
        ):
            logged_sequence = latest.sequence
            frames_log.write(
                json.dumps(
                    {
                        "t_host_s": round(time.monotonic() - started_monotonic, 3),
                        "t_device_ms": latest.timestamp_ms,
                        "seq": latest.sequence,
                        "broadband_dbfs": latest.broadband_dbfs,
                        "voice_likelihood": latest.voice_likelihood,
                        "vocal_ratio": latest.vocal_ratio,
                        "spectral_flatness": latest.spectral_flatness,
                        "vocal_peakiness": latest.vocal_peakiness,
                        "excited_vocal_bands": latest.excited_vocal_bands,
                        "mode": latest.mode,
                        "environment": latest.environment,
                        "conversation": latest.conversation,
                        "media_percent": latest.media_percent,
                        "ambient_open": latest.ambient_open,
                        "own_voice_confidence": latest.own_voice_confidence,
                        "own_voice_source": latest.own_voice_source,
                        "context": latest.context,
                        "context_score": latest.context_score,
                        "band_dbfs": list(latest.ambient_band_dbfs),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if player_service is not None:
            # 决策的形状永远来自 S3，这里只是把它搬到音频上；apply_decision 内部有节流。
            # 旧固件不发 eq_gain_db（tuning 为 None）时下发全平直：**没有建议就该什么都不做**，
            # 而不是把上一次的形状留在曲子上。
            if latest is not None:
                bands = list(latest.tuning.band_gain_db) if latest.tuning else [0.0] * 24
                player_service.apply_decision(bands, float(latest.media_percent))
        if ambient is not None:
            # S3 的 ambient_open 意图 → 执行端（relay 门控 / AirPods 聆听模式）。三条安全规则：
            #   * 旧固件（v4 之前）不报 ambient_open → 当成"不开放"，不猜；
            #   * 设备静默/掉线 → 必须关回去，不能把人留在环境声里；
            #   * 执行端自己异步、失败有 last_error，面板分开显示意图与执行。
            want_open = False
            if latest is not None and latest.ambient_open is not None:
                want_open = bool(latest.ambient_open)
            # 首个 self/other 判定出来前，不执行任何开放意图：串口刚打开可能还残留
            # 刷机前的旧命令帧，拿旧帧开麦比不开放更糟。
            # self：佩戴者这一轮不送进耳机（D28 self-veto；设备侧也会按同一判定不压/
            # 不抬），等 other 再开，形成按说话人轮流开放的中继；verdict 过期后
            # worker 不再给新判定，这里按"未就绪"处理（与固件 3 秒过期一致）。
            if voiceprint is not None:
                voiceprint_state = voiceprint.snapshot()
                if voiceprint_state.get("verdict") is None:
                    want_open = False
                elif voiceprint_state.get("verdict") == "self":
                    want_open = False
            if state.is_stale() or state.is_silent() or not state.connected:
                want_open = False
            ambient.set_open(want_open)
        # 50 ms 足够跟上 10 Hz 设备帧，又把"意图→执行"的额外延迟从 100 ms 压到 50 ms。
        time.sleep(0.05)

    # 收尾只有一份：与 /api/stop 的 on_stop 是同一个幂等 cleanup。
    # 先停 HTTP 线程不再接新请求，再跑收尾；收尾必须在 source_thread 退出前跑——
    # 串口还连着，才能把 `voiceprint off` 发给设备。
    # （cleanup 内部按 ambient → player → voiceprint off → worker → relay → 日志 的顺序。）
    httpd.shutdown()
    cleanup()
    for error in cleanup.errors:  # type: ignore[attr-defined]
        print(f"退出收尾失败：{error}", file=sys.stderr)
    source_thread.join(timeout=3.0)
    if relay is not None:
        live = relay.live() or {}
        stats = relay.stats()
        if relay.failure:
            thread_error.append(RelayError(relay.failure))
        elif not args.quiet:
            print(
                f"音频中继：播放 {stats.chunks_written} 包，队列积压丢弃 {stats.chunks_dropped} 包，"
                f"超出延迟预算丢弃 {stats.chunks_stale} 包。"
            )
            if live.get("measurable"):
                print(
                    f"          设备「{live.get('device')}」排队 {live.get('queued_ms', 0):.0f} ms，"
                    f"驱动侧 {live.get('driver_ms', 0):.0f} ms，"
                    f"合计约 {live.get('est_out_ms', 0):.0f} ms；"
                    f"播放端丢弃 {live.get('dropped_ms', 0):.0f} ms，"
                    f"启动排空 {live.get('drained_ms', 0):.0f} ms。"
                )

    if thread_error:
        error = thread_error[0]
        print(f"\n数据源中止：{error}", file=sys.stderr)
        return 3 if isinstance(error, MissingDependency) else 1
    if not args.quiet:
        snapshot = state.snapshot()
        print(
            f"\n已停止。累计收到 {snapshot['stats']['commands']} 帧，"
            f"丢 {snapshot['stats']['frames_lost']} 帧。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
