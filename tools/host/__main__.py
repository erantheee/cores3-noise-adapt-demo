"""上位机：把 CoreS3 的控制帧变成一个看得见、也能回控的面板。

    python3 -m host                 # 串口真机（自动找端口）
    python3 -m host --mock          # 没有设备也能先把界面跑起来
    python3 -m host --replay 记录/x.log --speed 4

启动后打开 http://127.0.0.1:8787/ 看面板。

退出码：0 正常停止 · 1 运行期错误（含设备不在场）· 2 参数错误 · 3 缺少依赖
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import webbrowser

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
        help=f"播放器音量百分比，默认 {DEFAULT_VOLUME_PERCENT:.0f}，可在面板上调节",
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
            f"中继要钉住的输出设备名（子串匹配），默认 {DEFAULT_DEVICE}。"
            "只钉这条流的输出，不改系统默认输出；设备不在时会如实报警"
        ),
    )
    parser.add_argument(
        "--relay-target-ms",
        type=float,
        default=DEFAULT_TARGET_MS,
        help=f"播放端允许的最大排队深度（毫秒），默认 {DEFAULT_TARGET_MS:g}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = build_source(args)
    relay = (
        AudioRelay(
            player=args.relay_player,
            device=args.relay_device,
            target_ms=args.relay_target_ms,
        )
        if args.relay_audio
        else None
    )
    if relay is not None:
        try:
            relay.start()
        except RelayError as exc:
            print(f"无法启动音频中继：{exc}", file=sys.stderr)
            return 3
    state = HostState(
        source=source.name,
        detail=source.detail,
        on_mic_chunk=(lambda chunk: relay.feed(chunk.pcm, chunk.rate)) if relay else None,
    )
    if relay is not None:
        # 中继的数字要进快照：这条通路上原来最缺的就是"现在到底晚了多少"。
        # 顺序要紧：attach 必须发生在 HostState 建好之后（这里曾经写成之前，结果是
        # 启动就 UnboundLocalError —— 而所有单测都测不到 main()，是启动横幅救的场）。
        state.attach_relay(relay)
    broadcaster = Broadcaster(state)
    writer = DeviceWriter()

    stop = threading.Event()

    def request_stop(signum=None, frame=None) -> None:  # noqa: ANN001, ARG001
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    # 只有串口源可回控。模拟源与回放都没有设备可写，界面会据此禁用按钮。
    can_control = isinstance(source, SerialSource)
    if can_control:
        source.on_open = writer.attach
        source.on_close = writer.detach

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

    httpd = make_server(
        state,
        broadcaster,
        web_port,
        writer=writer if can_control else None,
        releaser=release_port,
        player=player_service,
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
            print("  中继      未开启（加 --relay-audio）")
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

    # 主线程只做一件事：按节拍发布快照，并把最新一帧的决策下发给 Mac 执行端。
    # 没有新帧时也发快照，界面才能区分"设备停了"与"浏览器和主机之间的连接断了"——
    # 这两种情况的处置完全不同。
    while not stop.is_set():
        broadcaster.publish()
        if player_service is not None:
            # 决策的形状永远来自 S3，这里只是把它搬到音频上；apply_decision 内部有节流。
            # 旧固件不发 eq_gain_db（tuning 为 None）时下发全平直：**没有建议就该什么都不做**，
            # 而不是把上一次的形状留在曲子上。
            latest = state.latest
            if latest is not None:
                bands = list(latest.tuning.band_gain_db) if latest.tuning else [0.0] * 24
                player_service.apply_decision(bands, float(latest.media_percent))
        time.sleep(0.1)

    httpd.shutdown()
    source_thread.join(timeout=3.0)
    if player_service is not None:
        player_service.stop()
    if relay is not None:
        live = relay.live() or {}
        relay.stop()
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
