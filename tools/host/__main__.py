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
import math
import signal
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

from .ambient import AmbientService, RelayAmbient
from .assistant import AssistantService
from .audio_relay import (
    DEFAULT_DEVICE,
    DEFAULT_TARGET_MS,
    PLAYER_FFPLAY,
    PLAYER_SINK,
    AudioRelay,
    RelayError,
)
from .mac_audio import (
    DEFAULT_EARPHONES,
    Earphone,
    default_output,
    match_earphone,
    route_command,
)
from .player import DEFAULT_VOLUME_PERCENT, PlayerService
from .systemeq import SystemEqService
from .continuous_audio import ContinuousAudioWriter
from .server import Broadcaster, make_server, pick_port
from .training import TrainingWriter
from .voiceprint import VoiceprintService
from .sources import (
    MissingDependency,
    MockSource,
    ReplaySource,
    SerialSource,
    SourceError,
    build_source,
)
from .eq_profile import protected_bands
from .leak import estimate_leak
from .media import describe_media, estimate_content_kind
from .memo import MemoStore
from .state import CONTEXT_TEXT, HostState, environment_text

DEFAULT_WEB_PORT = 8787
#: 用户记忆目录（gitignore）：rolling + sessions + profile 都在这里。
DEFAULT_MEMO_DIR = str(Path(__file__).resolve().parents[2] / "记录" / "memo")


def masking_depth(mode: str | None, broadband_dbfs: float | None,
                  base: float = 1.3, peak: float = 2.5) -> float | None:
    """掩蔽自适应深度（2026-10-23 用户口径："噪声什么样、多大声，你来调节"）。

    背景人声（S3 的 EXTERNAL_VOICE_INTENT）时，掩蔽处方是 1–4 kHz 拉满 + 全段 +1；
    基础档平时 1.3× 是"自然"，但掩蔽要盖住人声，幅度应跟噪声大小连续走：
    安静（-54 dBFS）1.3× → 嘈杂（-30 dBFS）2.5×。非掩蔽模式返回 None（不覆盖面板深度）。
    """
    if mode != "EXTERNAL_VOICE_INTENT":
        return None
    if broadband_dbfs is None:
        return peak
    t = max(0.0, min(1.0, (broadband_dbfs + 54.0) / 24.0))
    return base + (peak - base) * t


def song_eq_depth(broadband_dbfs: float | None, panel_depth: float) -> float:
    """基础 EQ 的噪声连续深度（Layer 0，2026-10-30）。

    面板档位决定基准（basic 1.3× / 激进 2.5×），噪声决定起伏：
    安静（≤ -52 dBFS）0.8× 面板深度 → 嘈杂（≥ -30 dBFS）1.2×。
    这样"噪声多大推多少"是连续量，而不是固定两档；上限夹到 3.0×，
    避免激进档 + 高噪声把削减推到刺耳。基础层的正增益仍由执行端 5/6 dB 预算封顶。
    """
    if broadband_dbfs is None:
        return float(panel_depth)
    exposure = max(0.0, min(1.0, (float(broadband_dbfs) + 52.0) / 22.0))
    return max(0.8, min(3.0, float(panel_depth) * (0.8 + 0.4 * exposure)))


def stem_environment_for(frame) -> str:
    """把 S3 已确认的环境/事件映射成分轨策略；未知值保守回到宽带噪声。"""
    event = getattr(frame, "event", None) if getattr(frame, "event_valid", False) else None
    if event == "wind":
        return "wind"
    if event == "keyboard":
        return "keyboard"
    if event == "speech" or getattr(frame, "environment", None) == "VOICE":
        return "babble"
    if getattr(frame, "environment", None) == "QUIET":
        return "quiet"
    return "broadband"


# 低延迟实时主链的环境处方。这些值是“策略上限”，不是把
# S3 旧的 24 段建议照搬进歌曲。幅度刻意收在 ±0.8 dB 内：Mac 还会
# 根据高频密度、瞬态和输出峰值继续收缩；证据不足就接近原声。
_LOW_LATENCY_PROFILES: dict[str, tuple[float, ...]] = {
    "quiet": (0.0,) * 24,
    "keyboard": (
        -0.2, -0.2, -0.1, -0.1, 0.0, 0.0, 0.0, 0.0,
        0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1,
        0.0, 0.0, -0.1, -0.1, -0.2, -0.2, -0.2, -0.2,
    ),
    "wind": (
        -0.8, -0.7, -0.6, -0.5, -0.3, -0.2, -0.1, 0.0,
        0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5, 0.4,
        0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3, -0.3,
    ),
    "babble": (
        -0.5, -0.5, -0.4, -0.3, -0.2, -0.1, 0.0, 0.1,
        0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.8,
        0.7, 0.5, 0.3, 0.1, 0.0, -0.1, -0.2, -0.2,
    ),
    "broadband": (
        -0.3, -0.3, -0.2, -0.2, -0.1, 0.0, 0.0, 0.1,
        0.2, 0.3, 0.4, 0.5, 0.5, 0.6, 0.6, 0.6,
        0.5, 0.3, 0.1, 0.0, -0.1, -0.2, -0.3, -0.4,
    ),
}


def low_latency_strategy_bands(frame) -> list[float]:
    """S3 环境→克制的实时 DSP 处方；不依赖分轨，不增加窗等待。"""
    if getattr(frame, "mode", None) == "FLAT":
        return [0.0] * 24
    environment = stem_environment_for(frame)
    profile = _LOW_LATENCY_PROFILES[environment]
    level = getattr(frame, "broadband_dbfs", None)
    if not isinstance(level, (int, float)):
        exposure = 0.5
    else:
        # -58 dBFS 以下基本不动，-36 dBFS 到达处方上限。
        exposure = max(0.0, min(1.0, (float(level) + 58.0) / 22.0))
    return [round(value * exposure, 3) for value in profile]


#: 面板「EQ 档位」到掩蔽剂量的映射（2026-10-30 用户要求保留三级档位）：
#: 基础 1.0×（可听档，约 2.15 dB 目标），激进 1.4×（约 3 dB，更明显），
#: 关闭EQ 走执行端 bypass（原声），不经过剂量。真正的上限仍由段别 budget、
#: 内容瞬态门与输出余量兜住——剂量只是把目标调高，不是绕过安全门。
MASKING_DOSE_BASIC = 1.0
MASKING_DOSE_AGGRESSIVE = 1.4


def masking_dose_for(mode: str | None) -> float:
    """把面板的 EQ 档位映射成掩蔽剂量。未知档位退回 1.0（保守）。"""
    return MASKING_DOSE_AGGRESSIVE if str(mode or "") == "aggressive" else MASKING_DOSE_BASIC


def ambient_spectrum_for(frame) -> list[float]:
    """返回可供实时掩蔽矩阵使用的 S3 24 段环境谱；坏帧保守禁用。"""
    raw = getattr(frame, "ambient_band_dbfs", ())
    if len(raw) != 24:
        return []
    values: list[float] = []
    for value in raw:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return []
        value = float(value)
        if not math.isfinite(value):
            return []
        values.append(value)
    # 固件用 -120 dBFS 表示没有有效能量；整条地板不能驱动调音。
    return values if max(values) > -119.0 else []


def apply_neutral_execution(player_service, systemeq_service) -> bool:
    """暂停时把执行端复位到"什么都不做"：EQ 全平直、媒体 100%。

    只复位一次（主循环负责在暂停沿调用）。环境声由主循环同时关掉。
    返回值表示是否真的下发成功——没有执行端时返回 False，不伪报成功。
    """
    flat = [0.0] * 24
    if systemeq_service is not None:
        return bool(systemeq_service.apply_decision(flat, 100.0, base_gain=0.0))
    if player_service is not None:
        return bool(player_service.apply_decision(flat, 100.0))
    return False


def execution_block_reason(state, latest, execution_paused: bool) -> str | None:
    """返回不能执行 S3 处方的原因；None 表示控制数据新鲜。"""
    if execution_paused:
        return "manual_pause"
    if not state.connected:
        return "disconnected"
    if latest is None:
        return "no_frame"
    if state.is_stale():
        return "stale"
    if state.is_silent():
        return "silent"
    return None


def voiceprint_allows_ambient(snapshot: dict, max_age_s: float = 3.0) -> bool:
    """只有 worker 就绪且判定为新鲜 other 时允许环境声执行。"""
    age = snapshot.get("age_s")
    return bool(
        snapshot.get("ready") is True
        and snapshot.get("verdict") == "other"
        and isinstance(age, (int, float))
        and 0.0 <= age <= max_age_s
    )


def fresh_self_veto(snapshot: dict, max_age_s: float = 3.0) -> bool:
    """新鲜 self 判定是否应该否决环境声。

    2026-10-21 之后环境声的**主授权**是 LLM 语义判定（D32）；WavLM 声纹降级为
    只否决"佩戴者自己在说"的一种安全信号：unknown/pending/过期都不阻断，避免
    声纹在真实房间噪声里一直 unknown 把整条功能锁死（现场实测 speech_ratio 长期
    低于阈值，旧 DialogueTurnGate 因此从未放行过）。
    """
    age = snapshot.get("age_s")
    return bool(
        snapshot.get("ready") is True
        and snapshot.get("verdict") == "self"
        and isinstance(age, (int, float))
        and 0.0 <= age <= max_age_s
    )


def relay_reports_audio(*, relay_audio: bool, relay_ambient: object | None) -> bool:
    """中继是否真的在送音频（决定横幅/面板/路由告警怎么说）。

    坑（2026-10-23 真机审计）：旧版只开 `--voiceprint` 时 main() 也会建一个 Relay 对象，
    它只是借"有 relay"来触发设备持续采集，**不喂音频**（base_feed=None）。如果拿
    `relay is not None` 当"中继已开"，面板会显示一个永远 0 字节的 24 ms 延迟，并在耳机
    没连时误报"中继正在播放到扬声器"。2026-10-26 起 voiceprint 不再建 relay（采集触发
    改由 capture_needed 决定），但上报口径仍只看真正会送音频的两个来源：`--relay-audio`
    或 `--ambient-executor relay|observe`。
    """
    return bool(relay_audio or relay_ambient is not None)


def ambient_gate_allows(*, device_intent: bool, llm_ok: bool, self_ok: bool,
                        intent_recent: bool, evidence_recent: bool,
                        dual_turn: bool = False, llm_veto: bool = False) -> bool:
    """环境声执行门放行判定（D42；2026-10-26 晚改为**仅 LLM 语义确认**）。

    为什么只剩 llm_ok 一个触发器：现场复现证明 device_intent 与 dual_turn 都会在
    **单人独白 / 背景人声**时误开环境声——固件 event/vl 在"近场语音+音乐"下把语音
    错判成键盘/风（device_intent 短暂误发），声纹把一个人连续说话切成 self+other
    假交替（dual_turn 误触）。LLM 语义判定（dialogue=yes，要求 self+other 有语义
    轮次）是唯一能区分"对话"与"独白/背景"的信号，所以环境声只由它放行。

    快触发（device_intent / dual_turn / self_ok）仍然驱动**媒体下压**（主循环里的
    engagement_latch / dual_turn_open 分支，见下方），只是不再开环境声。代价是环境声
    打开延迟 = LLM 往返（约 5–10 s）；准确优先于速度，这是用户本轮口径。
    LLM 明确否决（llm_veto）优先级最高（D34 迟到否决）。
    """
    if llm_veto:
        return False
    return bool(llm_ok)


def llm_ambient_authorization(assistant) -> dict:
    """环境声执行端看到的语义授权。

    * LLM 未配置 → 放行，让旧的设备意图/声纹门控继续工作（不伪造语义判定）。
    * LLM 已配置 → 完全以它的新鲜判定为准；没有判定/不确定/非对话/过期都 fail-closed。
    """
    if assistant is None or not bool(getattr(assistant, "configured", False)):
        return {
            "allow": True,
            "state": "not_configured",
            "reason": "LLM 未配置：环境声回退到设备意图/声纹门控",
        }
    authorize = getattr(assistant, "ambient_authorization", None)
    if authorize is None:
        return {
            "allow": False,
            "state": "unavailable",
            "reason": "LLM 服务没有提供环境声授权接口（fail-closed）",
        }
    return dict(authorize())


#: A1/D50：host→S3 迟到否决回写的 TTL 与续期间隔。
#: TTL 对齐 assistant 的判定新鲜期（`_DECISION_TTL_S` = 20 s）；续期取 1/4，host 每拍
#: 看到新鲜否决就重发，判定过期/撤销后下发 `dialog yes` 提前解除。
DIALOG_VETO_TTL_MS = 20000
DIALOG_VETO_REFRESH_S = 5.0


class DialogOverrideLink:
    """把 LLM/本地的迟到否决回写到 S3 快路径（A1/D50，控制行 `dialog no|yes`）。

    为什么需要它：D34 把 LLM 定位成"迟到否决"，但过去只能关 Mac 执行端的
    `ambient_open`，没有通道让 S3 自己放开快路径。连续电视/播客会让端侧对话态一直
    engaged（EQ 压平、D41/D48 掩蔽停摆），即使 LLM 判 `dialogue=no` 也纠正不回来。

    只回写**新鲜的语义否决**（`state == "llm_veto"` 且 `age_s <= ttl_s`）：
      * 过期后停止续期，交给 S3 的 TTL 自己解除（fail-open 回 D16 预判）——
        host 崩了或串口断了也不会把 EQ 永久压平；
      * **本地否决（`local_veto`）不回写**。2026-10-28 真机发现：ASR 段"无有效语音"
        几乎在每次静音都会产生本地否决，若也回写 S3，设备会在没人说话时被锁在
        "释放对话态"的 TTL 里；用户随后真的开口，端侧预判反而被压住，首响退化到
        LLM 往返。本地的"这段音频没抽出人声"不是"这不是对话"的语义证据。
    """

    def __init__(self, ttl_ms: int = DIALOG_VETO_TTL_MS,
                 refresh_s: float = DIALOG_VETO_REFRESH_S) -> None:
        self.ttl_ms = int(ttl_ms)
        self.refresh_s = float(refresh_s)
        self._sent = False
        self._sent_at = 0.0

    def update(self, authorization: dict, now: float) -> bytes | None:
        """返回这一拍该发给 S3 的控制行（bytes）；没有变化时返回 None。"""
        age = authorization.get("age_s")
        ttl_s = authorization.get("ttl_s")
        fresh = (
            isinstance(age, (int, float))
            and isinstance(ttl_s, (int, float))
            and 0.0 <= float(age) <= float(ttl_s)
        )
        semantic_veto = str(authorization.get("state") or "") == "llm_veto"
        veto = bool(authorization.get("veto")) and semantic_veto and fresh
        if veto:
            if not self._sent or now - self._sent_at >= self.refresh_s:
                self._sent = True
                self._sent_at = now
                return f"dialog no {self.ttl_ms}\r\n".encode("ascii")
            return None
        if self._sent:
            self._sent = False
            return b"dialog yes\r\n"
        return None

    def release(self) -> bytes | None:
        """退出/串口断开时撤销尚未过期的否决（没有发过则返回 None）。"""
        if not self._sent:
            return None
        self._sent = False
        return b"dialog yes\r\n"


#: 阶段二/B1（2026-10-30）：host→S3 「歌是我放的」保活。
#: 设备侧 v1 只落遥测（帧键 `music_playing`），不改对话/掩蔽判据；TTL 失效自动
#: fail-open 回"没在放歌"，host 崩了/串口断了不会把设备状态永久写错。
MUSIC_KEEPALIVE_TTL_MS = 3000
MUSIC_KEEPALIVE_REFRESH_S = 1.0


class MusicKeepalive:
    """把"Mac 正在放歌"用 `music on|off` 控制行续期给 S3。

    证据只认**执行端回执**：systemeq 报 `execution == "applied"`（引擎在跑、实测有
    信号）或本地播放器报 `playing`。不用"用户点了播放""曲目列表非空"当证据——
    设备侧拿这个旗标解释"我听到的歌可能是自己放的"，猜错比没有更糟。
    """

    def __init__(self, ttl_ms: int = MUSIC_KEEPALIVE_TTL_MS,
                 refresh_s: float = MUSIC_KEEPALIVE_REFRESH_S) -> None:
        self.ttl_ms = int(ttl_ms)
        self.refresh_s = float(refresh_s)
        self._sent_on = False
        self._sent_at = 0.0

    def update(self, playing: bool, now: float) -> bytes | None:
        """返回这一拍该发的控制行（bytes）；没有变化时返回 None。"""
        if playing:
            if not self._sent_on or now - self._sent_at >= self.refresh_s:
                self._sent_on = True
                self._sent_at = now
                return f"music on {self.ttl_ms}\r\n".encode("ascii")
            return None
        if self._sent_on:
            self._sent_on = False
            return b"music off\r\n"
        return None

    def release(self) -> bytes | None:
        """退出/串口断开时告诉 S3 歌不再由本机播放（没发过则返回 None）。"""
        if not self._sent_on:
            return None
        self._sent_on = False
        return b"music off\r\n"


def music_is_playing(systemeq_service, player_service) -> bool:
    """执行端实测在放歌吗？只认回执，不认意图。"""
    if systemeq_service is not None:
        snapshot = systemeq_service.snapshot() or {}
        if snapshot.get("available") is not False and snapshot.get("execution") == "applied":
            return True
    if player_service is not None:
        snapshot = player_service.snapshot() or {}
        if snapshot.get("playing") is True:
            return True
    return False


class DialogueTurnGate:
    """要求 self/other 轮次证据，避免单个环境人声把通透模式顶开。

    仅作为 **LLM 未配置时** 的历史回退门控保留；LLM 配置后授权以 LLM 判定为准。
    """

    def __init__(self, window_s: float = 12.0) -> None:
        self.window_s = window_s
        self.last_verdict: str | None = None
        self.last_self_at: float | None = None
        self.last_other_at: float | None = None
        self.last_sequence: int | None = None
        self.other_speech_frames = 0

    def allows(self, snapshot: dict, frame=None, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        age = snapshot.get("age_s")
        fresh_identity = bool(
            snapshot.get("ready") is True
            and snapshot.get("verdict") in ("self", "other")
            and isinstance(age, (int, float))
            and 0.0 <= age <= 3.0
        )
        if not fresh_identity:
            self.other_speech_frames = 0
            self._expire(now)
            return False
        verdict = snapshot.get("verdict")
        if verdict != self.last_verdict:
            self.last_verdict = verdict
            if verdict == "self":
                self.last_self_at = now
            elif verdict == "other":
                self.last_other_at = now
        self._expire(now)
        # 状态机的 LIKELY 会保持数秒，不能拿旧意图放行当前的键盘/风声。
        # 必须是三个不同设备帧连续报告强人声，且事件规则没有明确否决。
        sequence = getattr(frame, "sequence", None)
        if sequence != self.last_sequence:
            self.last_sequence = sequence
            current_other_speech = bool(
                verdict == "other"
                and float(getattr(frame, "voice_likelihood", 0.0)) >= 0.65
                and getattr(frame, "event", None) not in ("keyboard", "wind", "artifact")
            )
            self.other_speech_frames = self.other_speech_frames + 1 if current_other_speech else 0
        return bool(
            verdict == "other"
            and voiceprint_allows_ambient(snapshot)
            and self.last_self_at is not None
            and self.last_other_at is not None
            and abs(self.last_other_at - self.last_self_at) <= self.window_s
            and self.other_speech_frames >= 3
        )

    def _expire(self, now: float) -> None:
        if self.last_self_at is not None and now - self.last_self_at > self.window_s:
            self.last_self_at = None
        if self.last_other_at is not None and now - self.last_other_at > self.window_s:
            self.last_other_at = None


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

    def send_dialog(self, line: bytes) -> None:
        """把迟到否决回写发给 S3（A1/D50 的 `dialog no|yes [ttl_ms]` 控制行）。"""
        self(line)


def parse_earphone(raw: str) -> Earphone:
    """解析 `--earphone MATCH=LABEL`。

    显示名必须 ASCII：S3 的 `lv_font_ui_16` 是 115 字子集，中文设备名会渲染成方框。
    """
    match, sep, label = raw.partition("=")
    match, label = match.strip(), label.strip()
    if not sep or not match or not label:
        raise argparse.ArgumentTypeError(f"需要 MATCH=LABEL，收到 {raw!r}")
    if not label.isascii():
        raise argparse.ArgumentTypeError(
            f"S3 显示名必须是 ASCII（lv_font_ui_16 是固定汉字子集）：{label!r}"
        )
    return Earphone(match, label)


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
        "--leak-residual",
        choices=("off", "observe", "apply"),
        default="observe",
        help="歌曲漏音残差（B2）：off=不算；observe=只报遥测（默认）；apply=扣掉后再进掩蔽矩阵",
    )
    parser.add_argument(
        "--system-eq-app",
        default=None,
        help=(
            "启用第三方播放器 system EQ 执行端（tools/systemeq），tap 指定 bundle id 的音频"
            "过 24 段 EQ 后送当前耳机（例如 com.tencent.QQMusicMac）。启用后 S3 的 24 段"
            "决策改发到这条链，而不是 tools/player。"
        ),
    )
    parser.add_argument(
        "--eq-mode",
        choices=("basic", "aggressive"),
        default="aggressive",
        help=(
            "systemeq 的 demo 基线档位：basic＝网友共识克制版（±3 dB 量级），"
            "aggressive＝\"demo 要明显\"×2 版（默认）。面板可实时切换；"
            "完全关闭 EQ 用面板「关闭EQ」（bypass 直通）。"
        ),
    )
    parser.add_argument(
        "--stem-remix",
        action="store_true",
        help="对 QQ 音乐启用六分轨实时重混；S3 环境决定策略，旧 24 段 EQ 自动归零",
    )
    parser.add_argument(
        "--relay-audio",
        action="store_true",
        help="把 CoreS3 麦克风实时中继到当前耳机（--relay-device 可改目标）",
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
            "中继要钉住的输出设备名（子串匹配）；默认跟随系统默认输出，"
            "AirPods Pro 与 Xiaomi 耳夹哪副在用都能出声。"
            "只钉这条流的输出，不改系统默认输出；设备不在时会如实报警"
        ),
    )
    parser.add_argument(
        "--earphone",
        action="append",
        type=parse_earphone,
        default=None,
        metavar="MATCH=LABEL",
        help=(
            "可识别的耳机：设备名子串=显示名（显示名必须 ASCII），可重复；默认 "
            + "、".join(f"{e.match}={e.label}" for e in DEFAULT_EARPHONES)
            + "。大小写不敏感，按顺序取第一个命中；加新耳机只改参数不改代码"
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
        choices=("off", "relay", "observe", "airpods"),
        default="off",
        help=(
            "环境声执行端。off=只显示 S3 的 ambient_open 意图；"
            "relay=当前 demo 路径：S3 麦克风 → Mac → 耳机中继，S3 说开就放行音频"
            "（AirPods 约 211–231 ms、Xiaomi 耳夹约 385–405 ms，先把预判与开关跑通）；"
            "observe=只判定/记录，不输出环境声（ASR/LLM/记忆照常跑，但耳机里永远不加现实声音）；"
            "airpods=仅 TWS 时代：用兄弟工程 airpods-control 把 AirPods 切到通透模式"
            "（OWS 耳夹没有聆听模式，这条路径在 OWS 上不可用；D35；需要辅助功能权限）"
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
        "--voiceprint-learn",
        action="store_true",
        help=(
            "实验性：开启声纹原型在线积累（默认关）。2026-10-28 事故后默认不学——"
            "旧默认权重 0.08 的 EMA 会在十几分钟内把原型带离注册锚点，导致自己说话被判 other；"
            "worker 侧仍有 anchor_min_cos 兜底，冻结时快照会有 learn_frozen/drift"
        ),
    )
    parser.add_argument(
        "--ambient-gate",
        choices=("off", "llm", "voice"),
        default="off",
        help=(
            "环境声执行门（demo）。off=旧行为：S3 意图快路径直接执行；"
            "llm=只有 ASR/LLM 确认对话才执行（背景人声不会开门，但开门要等 5–10 s）；"
            "voice=voiceprint 判 self（你接话）或 LLM 确认才执行，背景人声不执行。"
            "voice 需要同时开 --voiceprint；未启用时自动退化为 llm"
        ),
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
    parser.add_argument(
        "--training-dir",
        default="",
        help=(
            "训练数据落盘目录（每个候选片段一条 JSONL + 默认保存 16 kHz WAV）。"
            "host_up.sh 真机默认指到 记录/训练数据/；传空字符串关闭。"
        ),
    )
    parser.add_argument(
        "--no-training-audio",
        action="store_true",
        help="训练数据只写 JSONL/遥测，不保存原始音频（隐私/磁盘考虑）",
    )
    parser.add_argument(
        "--no-training-data",
        action="store_true",
        help="完全不采集训练数据（覆盖 host_up.sh 的默认目录）",
    )
    parser.add_argument(
        "--continuous-audio",
        default="",
        help=(
            "连续音频落盘目录（默认关）：把中继的 16 kHz PCM 按到达顺序整段写成 WAV，"
            "轮转处样本连续、丢块显式记账。与 --training-dir 的 6 s 分段不同，"
            "它是无人工断裂的连续混合音频，供 TSE / 听感 A/B/C / 复盘使用。"
            "host_up.sh 用 CORES3_CONTINUOUS_AUDIO=1 打开到 记录/连续音频/。"
        ),
    )
    parser.add_argument(
        "--continuous-chunk-seconds",
        type=float,
        default=600.0,
        help="连续音频单个 WAV 的轮转长度（秒），默认 600（16 kHz 约 19 MB）",
    )
    parser.add_argument(
        "--memo-dir",
        default=DEFAULT_MEMO_DIR,
        help=(
            "用户记忆目录（默认 记录/memo/，已被 gitignore）；传空串关闭。"
            "记忆只进慢层 LLM prompt + 影子统计，不参与实时判定（D38）"
        ),
    )
    parser.add_argument(
        "--memo-retention-days",
        type=int,
        default=14,
        help="用户记忆的保留天数（rolling + profile 衰减前的窗口），默认 14",
    )
    parser.add_argument(
        "--no-memo",
        action="store_true",
        help="完全不记录/注入用户记忆（--memo-dir "" 的等价开关）",
    )
    args = parser.parse_args(argv)
    # `--earphone` 可重复；没给就用内置的两副耳机（Xiaomi 耳夹 + AirPods Pro）。
    args.earphones = tuple(args.earphone) if args.earphone else DEFAULT_EARPHONES
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 环境声执行门（D42，demo）：voice 依赖 voiceprint；没有就退化为 llm。
    # 必须放在横幅/回调之前定义：回调与启动说明都要读它。
    effective_gate = args.ambient_gate
    if effective_gate == "voice" and not args.voiceprint:
        effective_gate = "llm"
        print("注意：--ambient-gate voice 需要 --voiceprint；本次退化为 llm 门", file=sys.stderr)
    source = build_source(args)
    # relay 执行端需要中继基础设施；--relay-audio 只表示"开机就常开中继"。
    # 2026-10-26：voiceprint 不再借 relay 触发采集（采集触发改由 capture_needed 决定），
    # 避免 voiceprint-only 也拉起一个空转的 16 kHz relaysink 去开音频输出。
    relay_needed = (
        args.relay_audio
        or args.ambient_executor in ("relay", "observe")
    )
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
    relay_ambient = (
        RelayAmbient(relay)
        if args.ambient_executor in ("relay", "observe") and relay
        else None
    )
    # 上报口径 ≠ 建对象口径：voiceprint-only 现在**不再**建 relay（见 relay_needed），
    # 采集触发由 capture_needed 单独决定。
    relay_reports = relay_reports_audio(
        relay_audio=args.relay_audio, relay_ambient=relay_ambient
    )
    if args.ambient_executor in ("relay", "observe") and relay is None:
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
        voiceprint_kwargs = {
            "threshold": args.voiceprint_threshold,
            # 2026-10-28：默认不学（事故后）。要在线积累必须显式 --voiceprint-learn。
            "learn": bool(args.voiceprint_learn),
        }
        if args.voiceprint_python:
            voiceprint_kwargs["python"] = args.voiceprint_python
        if args.voiceprint_enroll_dir:
            voiceprint_kwargs["enroll_dir"] = args.voiceprint_enroll_dir
        voiceprint = VoiceprintService(**voiceprint_kwargs)
        voiceprint.start()

    # 连续音频（P0/P1 的数据基建）：与训练数据 6 s 分段并存，互不替代。
    # 分段有段间断裂，不能做 TSE / 听感 A/B/C；连续落盘才是"真实连续对话"。
    continuous = None
    if args.continuous_audio:
        try:
            continuous = ContinuousAudioWriter(
                args.continuous_audio, chunk_seconds=args.continuous_chunk_seconds
            )
        except (OSError, ValueError) as exc:
            print(f"连续音频目录不可用，本次不采集：{exc}", file=sys.stderr)

    # 采集触发条件（2026-10-26）：relay（中继/执行端）、连续音频、声纹，任一需要设备
    # 持续推流就 rec start。voiceprint-only 也在这里，不再借 relay（那会拉起空转的
    # 16 kHz relaysink 去开音频输出）。与 continuous_audio.py 的"不开 relay 也 rec start"
    # 同口径。
    capture_needed = relay is not None or continuous is not None or voiceprint is not None

    def on_mic_chunk(chunk):
        if base_feed is not None:
            base_feed(chunk)
        if voiceprint is not None:
            voiceprint.feed(chunk)
        if continuous is not None:
            continuous.feed(chunk)

    if base_feed is None and voiceprint is None and continuous is None:
        on_mic_chunk = None
    training = None
    if args.training_dir and not args.no_training_data:
        try:
            training = TrainingWriter(
                args.training_dir, audio=not args.no_training_audio
            )
        except OSError as exc:
            print(f"训练数据目录不可写，本次不采集：{exc}", file=sys.stderr)
    memo = None
    if not args.no_memo and args.memo_dir:
        try:
            memo = MemoStore(args.memo_dir, retention_days=args.memo_retention_days)
        except OSError as exc:
            print(f"用户记忆目录不可写，本次不记录：{exc}", file=sys.stderr)

    # 场景/环境的取法：端侧 9 类情境模型优先（有中文译名），否则退回环境四分类。
    # 它们引用下面才创建的 state —— 这些函数只在 assistant/主循环里被调用，届时 state
    # 一定已经赋值（与 relay 的 attach 顺序同理，写错顺序会在启动时就暴露）。
    # D46：混合环境（context_mixed）给 LLM/账本一个可读的复合场景名，
    # 而不是只给 top1 单标签。
    def current_scene() -> str:
        frame = state.latest
        if frame is None:
            return "未知"
        context = getattr(frame, "context", None)
        if context is not None and getattr(frame, "context_valid", False):
            label = CONTEXT_TEXT.get(str(context), "")
            if label:
                if getattr(frame, "context_mixed", False):
                    top2 = CONTEXT_TEXT.get(str(getattr(frame, "context_top2", "") or ""), "")
                    if top2:
                        return f"{label}（混合：{top2}）"
                return label
        return environment_text(frame.environment)

    def current_environment() -> str:
        frame = state.latest
        return environment_text(frame.environment) if frame is not None else ""

    assistant = AssistantService(
        voiceprint_snapshot=voiceprint.snapshot if voiceprint is not None else None,
        training=training,
        memo_context=(lambda: memo.prompt_context(scene=current_scene())) if memo is not None else None,
        on_decision=(
            lambda decision: memo.record_decision(
                decision, scene=current_scene(), environment=current_environment()
            )
        ) if memo is not None else None,
    )
    state = HostState(
        source=source.name,
        detail=source.detail,
        on_mic_chunk=on_mic_chunk,
    )
    if voiceprint is not None:
        voiceprint.activity_provider = lambda: state.latest
    state.attach_assistant(assistant)
    if memo is not None:
        state.attach_memo(memo)
    assistant.start()
    assistant_configured = assistant.configured
    # 中继/连续音频/声纹用的是无结束时间的 S3 音频流；设备若在上一次主机退出后仍处于
    # 录音态，重连时不一定再发 started。主机既然明确要采集（capture_needed），就直接把
    # 它登记为持续流，会话边界随后由 conversation 状态切分。
    # 2026-10-26：voiceprint-only 也登记——否则 observe_frame 会因 _recording=False 提前
    # return，ASR/LLM 侧拿不到候选段，voice 门退化的 llm 判定就永远不工作。
    if capture_needed:
        assistant.recording_started(16_000, 0)
    if relay_reports:
        # 中继的数字要进快照：这条通路上原来最缺的就是"现在到底晚了多少"。
        # 顺序要紧：attach 必须发生在 HostState 建好之后（这里曾经写成之前，结果是
        # 启动就 UnboundLocalError —— 而所有单测都测不到 main()，是启动横幅救的场）。
        # 只在真的送音频时 attach：voiceprint-only 的 relay 对象空转，不能进快照。
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
    #: A1/D50：LLM/本地迟到否决 → S3 快路径的回写通道（`dialog no|yes`）。定义在
    #: writer 旁边，下面的 on_close/cleanup 才能安全地撤销否决。
    dialog_link = DialogOverrideLink()
    #: 阶段二/B1：`music on|off` 保活（设备侧 v1 只遥测）。
    music_link = MusicKeepalive()

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
            if effective_gate == "voice" and verdict == "self":
                # demo（D42）：self 由主机当"你接话"证据用；回传设备会被 D31 当成
                # "自己说话"而否决对话，反而把环境声门锁死。
                return
            try:
                writer.send_voiceprint(
                    verdict if verdict in ("self", "other") else "pending", score
                )
                voiceprint.note_sent()
            except (RuntimeError, OSError):
                # 串口没打开/刚断开：声纹遥测继续，判定不硬塞。
                pass

        voiceprint.on_verdict = _send_voiceprint

    stop = threading.Event()
    dialogue_turn_gate = DialogueTurnGate()

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
            # A1/D50：尚未过期的 S3 快路径否决也要撤销，否则退出后 EQ 会被压平到 TTL 结束。
            send_voiceprint_off()
            try:
                line = dialog_link.release()
                if line is not None:
                    writer(line)
                # B1：退出时也要告诉设备"歌不再由本机播放"；否则 TTL 到期前设备
                # 一直以为漏音是自己的歌。
                line = music_link.release()
                if line is not None:
                    writer(line)
            except (RuntimeError, OSError):
                pass
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
        # /api/stop 会先走到这里再跑 cleanup；串口马上要关，先尽力把 off 发出去。
        # 正常退出走 cleanup 时也发一次，重复发送是幂等的（设备侧清空判定）。
        send_voiceprint_off()
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

    # 第三方播放器 system EQ 执行端。和 player 独立：它 tap 目标 App，不改 tools/player。
    systemeq_service = None
    if args.system_eq_app:
        # 基础档是"自然好听"档：S3 自适应的处方深度也按演示口径降一档（1.3×），
        # 否则 ×2.5 会把 S3 的 presence 建议放大到 +4~5，电吉他/MIDI 这类中高频
        # 密集素材会听出"裂开"（2026-10-22 用户听感）。面板仍可实时拉深度。
        eq_depth = 1.3 if args.eq_mode == "basic" else 2.5
        stem_worker = None
        if args.stem_remix:
            stem_worker = str(Path(__file__).resolve().parents[1] / "stem_remix" / "stream_worker.py")
        systemeq_service = SystemEqService(
            app=args.system_eq_app, mode=args.eq_mode, depth=eq_depth,
            demo=not args.stem_remix, stem_worker=stem_worker,
        )
        state.attach_systemeq(systemeq_service)

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
    if systemeq_service is not None:
        cleanup_steps.append(("QQ 音乐 system EQ", systemeq_service.stop))
    if voiceprint is not None:
        cleanup_steps.append(("声纹 off 回执", send_voiceprint_off))
        cleanup_steps.append(("声纹 worker", voiceprint.stop))
    if relay is not None:
        cleanup_steps.append(("音频中继", relay.stop))
    cleanup_steps.append(
        ("逐帧日志", lambda: frames_log.close() if frames_log is not None else None)
    )
    if memo is not None:
        cleanup_steps.append(("用户记忆", memo.finalize_session))
    if training is not None:
        cleanup_steps.append(("训练数据", training.close))
    if continuous is not None:
        cleanup_steps.append(("连续音频", continuous.close))
    cleanup = make_cleanup(cleanup_steps)

    httpd = make_server(
        state,
        broadcaster,
        web_port,
        writer=writer if can_control else None,
        releaser=release_port,
        player=player_service,
        systemeq=systemeq_service,
        on_stop=cleanup,
    )
    url = f"http://127.0.0.1:{web_port}/"

    if not args.quiet:
        print("=" * 66)
        print("CoreS3 上位机")
        print(f"  数据源    {state.source}  {source.detail or ''}")
        print("  耳机      自动识别：" + " / ".join(
            f"{e.label}（{e.match}）" for e in args.earphones))
        print(f"  面板      {url}")
        print("  帧率      设备每 100 ms 一帧；录音时放宽到 250 ms（预期行为）")
        print("  回控      可发 rec start [秒数] / rec stop" if can_control else "  回控      不可用（当前数据源没有设备可写）")
        if relay_reports:
            print(f"  中继      S3 麦克风 → {args.relay_device or '系统默认输出'}（播放端 {relay.mode}，"
                  f"排队上限 {args.relay_target_ms:g} ms）")
            if relay.mode == PLAYER_FFPLAY:
                print("  注意      ffplay 不报告排队深度：这条路的延迟只能靠声学测量，"
                      "面板会显示为「测不到」")
        else:
            print("  中继      未开启（加 --relay-audio 或 --ambient-executor relay）")
        if relay_ambient is not None:
            mode_text = (
                "只判定/记录，不输出（observe）"
                if args.ambient_executor == "observe"
                else "S3 麦克风中继到耳机"
            )
            print(
                f"  环境声    {mode_text}"
                f"（执行端 s3-relay，设备「{args.relay_device or '系统默认输出'}」，"
                f"排队上限 {args.relay_target_ms:g} ms）"
            )
            print("  注意      中继时延 = 播放端排队 + 当前耳机的 driver_ms（面板实时显示）："
                  "AirPods Pro 170.7 ms（总≈211–231），Xiaomi 耳夹 324.8 ms（总≈385–405，"
                  "2026-10-21 实测）；这一版只验证预判与开关，不宣称能听清对面")
            if assistant_configured:
                print("  授权      环境声还必须由 ASR/LLM 判定为对话且未过期；"
                      "不确定/非对话/过期一律关闭（D32）")
        elif isinstance(ambient, AmbientService):
            print(
                f"  环境声    S3 ambient_open 意图 → AirPods 聆听模式「{args.ambient_mode}」"
                f"（执行端 {ambient.cli_path}，仅 TWS；OWS 无聆听模式，D35）"
            )
            if not ambient.available:
                print("  注意      找不到 airpods-control CLI：意图会显示，但执行端不可用")
            if assistant_configured:
                print("  授权      环境声还必须由 ASR/LLM 判定为对话且未过期（D32）")
        else:
            print("  环境声    只显示 S3 意图（--ambient-executor relay|airpods 才执行）")
        if effective_gate != "off":
            print(
                f"  环境声门  {effective_gate}（"
                + ("voiceprint self / 你接话 或 LLM 确认才让位媒体/开环境声；背景人声两样都不做"
                   if effective_gate == "voice" else "LLM 确认才让位媒体/开环境声；背景人声两样都不做")
                + "，D42）"
            )
        if assistant_configured:
            print(f"  对话判定  {assistant.model} · {assistant._asr_model} 转写 · "
                  f"快路径端侧先开，LLM 迟到否决（D34）")
        else:
            print("  对话判定  未配置 LLM（ANTHROPIC_*）：环境声回退到设备意图/声纹门控")
        if training is not None:
            print(f"  训练数据  {training.session_dir}/"
                  f"（{'含音频' if training.audio_enabled else '仅遥测'}；"
                  f"上限 {training.max_bytes // (1024 * 1024)} MB）")
        else:
            print("  训练数据  未采集（--training-dir 指到目录才开）")
        if continuous is not None:
            print(f"  连续音频  {continuous.session_dir}/"
                  f"（16 kHz 单声道；{args.continuous_chunk_seconds:g} s/段轮转；"
                  f"丢块会记账，供 TSE / 听感）")
        else:
            print("  连续音频  未采集（CORES3_CONTINUOUS_AUDIO=1 或 --continuous-audio 打开）")
        if memo is not None:
            memory = memo.snapshot()
            print(f"  用户记忆  {memory['dir']}/"
                  f"（已恢复 {memory['events']} 条事件 / {memory['profile_keys']} 个场景仓位；"
                  "只进慢层先验，D38）")
        else:
            print("  用户记忆  未启用（--no-memo）")
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
                earphone = match_earphone(device, args.earphones)
                writer(route_command(device, args.earphones))
                last_device = device
                state.log("route", "Mac 音频输出已同步", device or "没有默认输出")
                # 真机踩过：耳机掉线后默认输出自动落到 MacBook Air 扬声器，中继照常播放，
                # 面板只记了一行路由变更。用户戴着耳机听不到声音时，会以为是自己这边的问题，
                # 于是往延迟、增益、缓冲的方向查——真正的原因（耳机压根没连）反而没人说。
                # 所以这里必须报警，而不是只记录。
                if relay_reports and earphone is None:
                    expected = "、".join(f"「{e.match}」" for e in args.earphones)
                    state.note(
                        f"中继正在播放到「{device or '没有默认输出'}」，不是已识别的耳机"
                        f"（匹配 {expected}）。声音不会进到你戴的耳机里。先确认耳机已连上"
                        "这台 Mac，并在「系统设置 → 声音 → 输出」里选中它；是别的耳机就加"
                        " --earphone MATCH=LABEL。"
                    )
            except (OSError, RuntimeError):
                continue

    if can_control:
        threading.Thread(target=monitor_mac_route, name="mac-route", daemon=True).start()

    def start_live_capture() -> None:
        """Start indefinite device capture after the serial console has finished booting."""
        if not capture_needed:
            return
        capture_reason = (
            "音频中继" if relay is not None
            else ("声纹采集" if voiceprint is not None else "连续音频")
        )
        deadline = time.monotonic() + 10.0
        while not stop.is_set() and time.monotonic() < deadline:
            if writer.available:
                time.sleep(0.8)
                try:
                    writer(b"rec start\r\n")
                    state.note_control_sent(f"rec start（{capture_reason}）")
                    return
                except (OSError, RuntimeError):
                    pass
            time.sleep(0.1)
        state.note("音频中继未能启动：10 秒内没有可写的设备连接")

    if can_control and capture_needed:
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
    last_ambient_open: bool | None = None
    last_authorization_state: str | None = None
    #: memo 跟踪的是 **S3 的 ambient_open 意图**（不管执行端开没开），因为"设备认为该不该
    #: 开"才是记忆要学的信号；执行结果与授权另存在事件里。
    memo_intent_open: bool | None = None
    memo_intent_opened_at: float | None = None
    last_authorization: dict = {}
    #: 环境声执行门（demo，D42）：voice 模式下"你接话"一次就把门栓住，
    #: 直到 S3 的环境声意图消失 2.5 s；避免背景人声把耳机门顶开。
    engagement_latch = False
    last_intent_true_at: float | None = None
    #: 门证据（self 接话 / LLM 确认）最近一次出现的时刻。2026-10-23 真机实测：S3
    #: 端侧 engaged 判据（voice_likelihood≥0.65 连续 800 ms）在真实语音下几乎不成立
    #: （最长连续只有 0.5 s），device_intent 恒 false；因此门证据本身必须能独立放行。
    last_gate_evidence_at: float | None = None
    last_gate_state: str | None = None
    #: 声纹双向交替触发（2026-10-23 B 方案）：本拍是否认定"对方找你、你接话"。
    #: 每拍在门逻辑里更新；media 分支在同一循环早段先读上一拍的值。
    dual_turn_open = False

    # 主线程只做一件事：按节拍发布快照，并把最新一帧的决策下发给 Mac 执行端。
    # 没有新帧时也发快照，界面才能区分"设备停了"与"浏览器和主机之间的连接断了"——
    # 这两种情况的处置完全不同。
    execution_paused_last = False
    # 初始按“尚未证明数据新鲜”处理；第一次循环必须主动复位执行端，不能让
    # systemeq 的默认 demo 曲线在等待首帧的几秒里短暂生效。
    execution_blocked_last = False
    while not stop.is_set():
        broadcaster.publish()
        latest = state.latest
        # 面板「暂停执行」（2026-10-26）：只停执行端，采集与 AI 继续跑。
        # 进入暂停时要**主动复位一次**执行端，否则上一帧的 EQ/下压会留在音频上，
        # 看起来像"暂停键没生效"。
        execution_paused = bool(getattr(state, "execution_paused", False))
        if execution_paused and not execution_paused_last:
            neutral_ok = apply_neutral_execution(player_service, systemeq_service)
            if ambient is not None:
                ambient.set_open(False)
                last_ambient_open = False
            state.note(
                "演示已暂停：执行端已复位（EQ 平直 / 媒体 100%）"
                + ("、环境声已关闭" if ambient is not None else "")
                + "；采集与 AI 继续"
                + ("" if neutral_ok or (player_service is None and systemeq_service is None)
                   else "（执行端复位未确认，请检查面板 Mac 实际应用一栏）")
            )
        elif not execution_paused and execution_paused_last:
            state.note("演示已继续：执行端恢复跟随 S3 决策")
        execution_paused_last = execution_paused
        block_reason = execution_block_reason(state, latest, execution_paused)
        execution_blocked = block_reason is not None
        # 1 秒无帧就像手动暂停一样复位一次，不能无限沿用最后一帧处方。
        if execution_blocked and not execution_blocked_last and not execution_paused:
            neutral_ok = apply_neutral_execution(player_service, systemeq_service)
            if ambient is not None:
                ambient.set_open(False)
                last_ambient_open = False
            state.note(
                "S3 控制数据中断：调音已旁路为原声、媒体 100%"
                + ("、环境声已关闭" if ambient is not None else "")
                + ("" if neutral_ok or (player_service is None and systemeq_service is None)
                   else "（执行端复位未确认）")
            )
        elif not execution_blocked and execution_blocked_last:
            state.note("S3 控制数据恢复：调音重新跟随实时决策")
        execution_blocked_last = execution_blocked
        if memo is not None and latest is not None:
            # 每 10 秒采一次环境统计（memo 内部节流）；只进长期 profile，不参与决策。
            memo.sample_frame(
                scene=current_scene(),
                environment=current_environment(),
                broadband_dbfs=latest.broadband_dbfs,
                voice_likelihood=latest.voice_likelihood,
            )
        if (
            memo is not None
            and latest is not None
            and latest.ambient_open is not None
        ):
            # D38：记录的是 S3 的意图转变（默认执行端 off 时唯一的可观测信号）。
            # 执行端开了的话，授权状态会作为同一事件的附件写进去。
            intent = bool(latest.ambient_open)
            if intent:
                last_intent_true_at = time.monotonic()
            if memo_intent_open is None:
                memo_intent_open = intent
                memo_intent_opened_at = time.monotonic() if intent else None
            elif intent != memo_intent_open:
                mono_now = time.monotonic()
                if intent:
                    memo_intent_opened_at = mono_now
                    memo.record_ambient(
                        open=True,
                        scene=current_scene(),
                        environment=current_environment(),
                        intent=intent,
                        authorization=last_authorization,
                    )
                else:
                    opened_at = memo_intent_opened_at
                    memo_intent_opened_at = None
                    memo.record_ambient(
                        open=False,
                        scene=current_scene(),
                        environment=current_environment(),
                        intent=intent,
                        authorization=last_authorization,
                        duration_s=None if opened_at is None else max(0.0, mono_now - opened_at),
                    )
                memo_intent_open = intent
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
        if not execution_blocked and latest is not None and (player_service is not None or systemeq_service is not None):
            # 本地播放器仍保留 S3 旧 24 段通路；QQ 音乐实时主链则只读 S3
            # 的环境意图，转成小幅低延迟处方，不再照搬粗暴 EQ 形状。
            bands = list(latest.tuning.band_gain_db) if latest.tuning else [0.0] * 24
            media_target = float(latest.media_percent)
            if effective_gate != "off" and not engagement_latch:
                # 同一道执行门也管媒体下压：背景人声/视频声不该把歌压小。
                # 只在你接话（voiceprint self）或 LLM 确认对话之后才让位；
                # 门的证据来自上一拍（50 ms），对让位时序没有可感知影响。
                media_target = 100.0
            elif dual_turn_open:
                # 声纹交替触发时 S3 意图可能没发（设备 vl 判据不可达，12:02 复盘），
                # 媒体也要让位到确认地板，否则音乐会盖住对方的话。
                media_target = 60.0
            if systemeq_service is not None:
                ambient_spectrum = ambient_spectrum_for(latest)
                # B2：扣掉 Mac 自己放的歌。默认 observe 只报遥测——没有真机 A/B 之前
                # 不动调音动作；apply 才把残差谱送进矩阵。
                if (
                    ambient_spectrum
                    and args.leak_residual != "off"
                    and music_is_playing(systemeq_service, player_service)
                ):
                    # 内容谱必须新鲜：歌停了之后 systemeq 的状态还留着上一次的谱形，
                    # 拿过期谱形去"解释"当前环境会把真实噪声当成漏音减掉。
                    side = systemeq_service.snapshot() if systemeq_service is not None else {}
                    age = (side or {}).get("state_age_s")
                    # 内容谱只来自 systemeq 的 tap：本地播放器出声时那条链是静音的，
                    # 拿它的谱形去解释环境等于用过期数据做减法——必须要求 execution=applied。
                    fresh = (
                        (side or {}).get("execution") == "applied"
                        and isinstance(age, (int, float))
                        and 0.0 <= float(age) <= 2.0
                    )
                    content_shape = (side or {}).get("content_band_shape_db") if fresh else None
                    leak = estimate_leak(
                        ambient_spectrum,
                        content_shape or [],
                        enabled=args.leak_residual == "apply",
                    )
                    state.set_leak({
                        "mode": args.leak_residual,
                        "applied": leak.applied,
                        "reason": leak.reason or "ok",
                        "leak_level_db": leak.leak_level_db,
                        "confidence": leak.confidence,
                        "spread_db": leak.spread_db,
                    })
                    if leak.applied and len(leak.bands) == 24:
                        ambient_spectrum = leak.bands
                # 2026-10-30 用户："以前 QQ 音乐 100% 都能解决，现在又是毛又是变小"。
                # 回归 D54（ed01782）链路：S3 处方 × 深度 + demo 基线 + 削波收缩 trim，
                # **没有**全局余量压音量。环境×内容矩阵停用（代码与开关预留，B3 后再议）。
                masking_enabled = False
                if args.stem_remix:
                    systemeq_service.set_stem_environment(stem_environment_for(latest))
                else:
                    # D54 行为：决策形状永远来自 S3 处方（含键盘/EVI 事件包络）。
                    # 类型保护（Layer 1）只加 ±1～2 dB 段偏移，不替代基础 EQ。
                    bands = list(latest.tuning.band_gain_db) if latest.tuning else [0.0] * 24
                    kind = estimate_content_kind(systemeq_service.snapshot() or {}).get("kind")
                    bands = protected_bands(bands, kind)
                # D54 行为：EVI（背景人声掩蔽）按噪声连续加深 1.3→2.5×；其余用面板深度。
                depth_override = masking_depth(latest.mode, latest.broadband_dbfs)
                # D54 行为：demo 基线恢复（FLAT/安静时归零）——旧版"一耳朵能听出来"
                # 的来源之一。类型保护与深度在它之上叠加。
                base_gain = 0.0 if latest.mode == "FLAT" else 1.0
                # 面板档位 → 掩蔽剂量：基础 1.0×、激进 1.4×（关闭EQ 走 bypass）。
                systemeq_mode = (systemeq_service.snapshot() or {}).get("mode")
                systemeq_service.apply_decision(bands, media_target,
                                                depth_override=depth_override,
                                                base_gain=base_gain,
                                                ambient_band_dbfs=ambient_spectrum,
                                                ambient_broadband_dbfs=latest.broadband_dbfs,
                                                masking_enabled=masking_enabled,
                                                masking_dose=masking_dose_for(systemeq_mode))
            else:
                player_service.apply_decision(bands, media_target)
        # 面板「当前媒体」卡（2026-10-30 用户口径）：媒体源 / 内容类型（估计）/
        # 调音思路。判断全在这里做，面板只负责显示。执行端暂停时也照实报，
        # 但要说清"现在听到的是原声"。
        try:
            state.set_media(describe_media(
                systemeq=systemeq_service.snapshot() if systemeq_service is not None else None,
                player=player_service.snapshot() if player_service is not None else None,
                frame=None if latest is None else {
                    "mode": latest.mode,
                    "broadband_dbfs": latest.broadband_dbfs,
                    # 可听的基础层：S3 24 段处方（面板要能写出"到底调了多少"）。
                    "tuning_bands": list(latest.tuning.band_gain_db) if latest.tuning else [],
                    # 掩蔽层 B 的事件证据（S3 侧的包络已合进处方，面板只需要说明来源）。
                    "event": latest.event,
                    "typing_active": bool(latest.typing_active),
                },
                leak=getattr(state, "leak", None),
            ))
        except Exception:  # noqa: BLE001 - 媒体卡不是关键路径，绝不能因此停主循环
            state.set_media(None)
        # A1/D50：把 LLM/本地的迟到否决回写 S3 快路径。**与执行端配置无关**——
        # 默认 `--ambient-executor off` 的演示最需要它：环境声本来就关着，但 S3 的
        # 对话态会把歌曲 EQ 压平、把 D41/D48 的掩蔽停掉，LLM 的 no 必须能纠正快路径。
        if (
            not execution_paused
            and can_control
            and assistant_configured
            and writer.available
        ):
            try:
                line = dialog_link.update(
                    llm_ambient_authorization(assistant), time.monotonic()
                )
                if line is not None:
                    writer(line)
                    state.note_control_sent(
                        "dialog no（LLM 迟到否决 → S3 快路径释放）"
                        if line.startswith(b"dialog no")
                        else "dialog yes（撤销 S3 快路径否决）"
                    )
            except (RuntimeError, OSError):
                # 串口刚断开：回写不是关键路径，下一拍重试，不阻断主循环。
                pass
        # 阶段二/B1：把"Mac 正在放歌"续期给 S3（默认每 1 s）；停止时发一次 off。
        # 它与执行门无关——暂停执行时歌其实还在放，所以只在暂停时停止续期，
        # 让设备 TTL 自己失效，不硬发 off（暂停仍可能在放歌给用户听）。
        if not execution_paused and can_control and writer.available:
            try:
                line = music_link.update(
                    music_is_playing(systemeq_service, player_service),
                    time.monotonic(),
                )
                if line is not None:
                    writer(line)
                    state.note_control_sent(
                        "music on（S3 记录「歌是 Mac 放的」）"
                        if line.startswith(b"music on")
                        else "music off（歌已停 / 执行端关闭）"
                    )
            except (RuntimeError, OSError):
                pass
        if ambient is not None and not execution_paused:
            # S3 的 ambient_open 意图 → 执行端（relay 门控 / AirPods 聆听模式）。安全规则：
            #   * 旧固件（v4 之前）不报 ambient_open → 当成"不开放"，不猜；
            #   * 设备静默/掉线 → 必须关回去，不能把人留在环境声里；
            #   * execution 端自己异步、失败有 last_error，面板分开显示意图与执行。
            want_open = False
            if latest is not None and latest.ambient_open is not None:
                want_open = bool(latest.ambient_open)
            authorization: dict = {}
            if assistant_configured:
                # D34：端侧快路径先开，LLM 只有迟到否决权。任何网络/ASR 延迟都不
                # 能进入首次响应路径；LLM 判 no 时在这里把已开的环境声关掉。
                authorization = llm_ambient_authorization(assistant)
                want_open = want_open and bool(authorization.get("allow"))
                if effective_gate == "off" and voiceprint is not None and fresh_self_veto(
                    voiceprint.snapshot()
                ):
                    want_open = False
            elif voiceprint is not None:
                # LLM 未配置时保留历史回退：新鲜 other + 双方轮次证据才放行。
                voiceprint_state = voiceprint.snapshot()
                want_open = want_open and dialogue_turn_gate.allows(voiceprint_state, latest)
            intent_open = want_open
            gate_state = "off"
            if effective_gate != "off":
                # D42（demo）：背景人声不应把环境声顶开。门只拿两类证据：
                #   * ASR/LLM 确认这是一个对话；
                #   * voiceprint 判 self —— 也就是"你接话了"。
                # 一旦触发，门栓到 S3 意图消失 2.5 s，避免一句一关。
                llm_ok = str(authorization.get("state") or "") == "llm_confirmed"
                self_ok = False
                dual_turn_open = False
                if effective_gate == "voice" and voiceprint is not None:
                    vp = voiceprint.snapshot()
                    age = vp.get("age_s")
                    self_ok = bool(
                        vp.get("ready")
                        and vp.get("verdict") == "self"
                        and isinstance(age, (int, float))
                        and 0.0 <= age <= 3.0
                    )
                    # B 方案（2026-10-23 用户拍板）：声纹双向交替作为环境声触发源。
                    # 最近 10 s（160000 样本 @16 kHz）内 self 与 other 都出现且交替，
                    # 视为"对方找你、你接话"的双向对话；只有 self 的自言自语不触发。
                    history = vp.get("history") or []
                    if history and isinstance(history[-1].get("end_sample"), int):
                        latest_end = int(history[-1]["end_sample"])
                        order = [
                            str(item["verdict"])
                            for item in history
                            if isinstance(item.get("end_sample"), int)
                            and latest_end - int(item["end_sample"]) <= 16000 * 10
                            and item.get("verdict") in ("self", "other")
                        ]
                        dual_turn_open = bool(
                            "self" in order and "other" in order
                            and any(a != b for a, b in zip(order, order[1:]))
                        )
                if llm_ok or self_ok or dual_turn_open:
                    engagement_latch = True
                    last_gate_evidence_at = time.monotonic()
                intent_recent = (
                    last_intent_true_at is not None
                    and time.monotonic() - last_intent_true_at <= 2.5
                )
                evidence_recent = (
                    last_gate_evidence_at is not None
                    and time.monotonic() - last_gate_evidence_at <= 8.0
                )
                if engagement_latch and not (intent_recent or evidence_recent):
                    engagement_latch = False
                # 2026-10-23：门是**否决器**，不是触发器。S3 端侧意图（device_intent）
                # 仍是环境声的唯一触发源；self 只能批准"别人发起的对话"，不能因为
                # 佩戴者自己说话就把环境声拉开（用户现场质疑过这一点）。
                # LLM 明确否决（llm_veto）优先级最高，self 证据不能覆盖（D34）。
                want_open = ambient_gate_allows(
                    device_intent=bool(want_open),
                    llm_ok=llm_ok,
                    self_ok=self_ok,
                    intent_recent=intent_recent,
                    evidence_recent=evidence_recent,
                    dual_turn=dual_turn_open,
                    llm_veto=bool(authorization.get("veto")),
                )
                gate_state = (
                    "user_replied"
                    if self_ok
                    else ("llm_confirmed" if llm_ok else ("latch" if engagement_latch else "waiting"))
                )
                authorization = dict(authorization)
                authorization["engagement"] = gate_state
            if state.is_stale() or state.is_silent() or not state.connected:
                want_open = False
            if args.ambient_executor == "observe":
                # 只判定/记录，不输出：observe 模式保留 ASR/LLM/记忆与面板意图，
                # 但永远不把现实声音送进耳机。给"怕环境声突然响"的场景用。
                want_open = False
            if (
                effective_gate != "off"
                and intent_open
                and not want_open
                and gate_state == "waiting"
                and last_gate_state != "waiting"
            ):
                state.note(
                    "环境声意图已发出，但没有等到你接话/LLM 确认，本次不执行"
                    f"（demo 执行门 {effective_gate}，D42）"
                )
            last_gate_state = gate_state
            ambient.set_open(want_open)
            last_authorization = authorization
            if training is not None and (
                want_open != last_ambient_open
                or str(authorization.get("state") or "") != (last_authorization_state or "")
            ):
                training.write_event("ambient", {
                    "open": bool(want_open),
                    "device_intent": None if latest is None else latest.ambient_open,
                    "authorization_state": authorization.get("state"),
                    "authorization_allow": authorization.get("allow"),
                    "veto": authorization.get("veto"),
                    "verdict": authorization.get("verdict_text"),
                    "decision_age_s": authorization.get("age_s"),
                    "decision_number": authorization.get("number"),
                    "reason": authorization.get("reason"),
                    "engagement": authorization.get("engagement"),
                    "gate": effective_gate,
                })
                last_ambient_open = bool(want_open)
                last_authorization_state = str(authorization.get("state") or "")
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
