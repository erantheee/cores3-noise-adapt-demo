"""用户记忆（memo）：把 LLM/决策的结果落盘，供下一次运行接着用。

为什么需要它：LLM 的跨片段 context 现在只在进程内存里（24 轮 + 4 次判定），换一次对话
或重启上位机就失忆。**要用 context 就必须有能恢复的 memo**；时间长了，这些记忆还要能作为
慢层先验进入决策（D38 的路线：存档 → 注入 → 影子 → 受限执行）。

量产的 AI 不是给每个用户换一份权重；它靠**同一套模型 + 每个用户自己的记忆/profile**
慢慢变得"量身定制"。所以这里存的是偏好状态与事件，而不是训练数据之外的黑箱。

三层，全部是可解释的数据，不是模型权重：

* **rolling**：最近事件（判定 / 环境声开关 / 用户纠正），留最近 N 条 + 保留期；
* **sessions**：每次 host 运行结束写一条结构化摘要（`sessions.jsonl`）；
* **profile**：按「场景 × 时段」的确定性统计，带半衰期，作为长期先验。

纪律（与 D34/D38 一致，写进代码而不是只写文档）：

1. **慢层专用**：本模块不进 100 ms 实时环；首次响应仍由端侧决定。
2. **只存文本与统计**：不存音频；目录默认 `记录/memo/`（已被 .gitignore 忽略）。
3. **可解释、可删除**：每个统计都带来源事件与更新时间；删掉 memo.json 即清空记忆。
4. **默认不直接执行**：先注入 LLM prompt（先验）+ 影子建议；真正进入执行要过 D38 闸门。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = 1

#: 场景×时段的仓位。时段只用于"同一场景在不同时间可能不是一个场景"，
#: 不引入任何日历/日程数据。
_TIME_BANDS = (
    ("夜间", 0, 6),
    ("早上", 6, 12),
    ("午后", 12, 18),
    ("傍晚", 18, 24),
)

#: 环境声"开了很快就关"的阈值：短于它算一次可疑开启（可能是误开）。
SHORT_OPEN_S = 3.0

#: 每次采样环境噪声的最小间隔。10 Hz 的帧没必要每帧都进统计。
SAMPLE_INTERVAL_S = 10.0

#: 注入 prompt 的硬上限。LLM 单次已经要 5 秒，记忆不能把 prompt 撑大。
PROMPT_MAX_CHARS = 300


def _band_label(ts: float) -> str:
    hour = datetime.fromtimestamp(ts).hour
    for label, start, end in _TIME_BANDS:
        if start <= hour < end:
            return label
    return "夜间"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _key(scene: str, band: str) -> str:
    return f"{scene or '未知'}|{band}"


class MemoStore:
    """用户记忆的落盘与聚合。线程安全；所有写入都走同一把锁 + 原子替换。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        retention_days: int = 14,
        max_events: int = 240,
        half_life_days: float = 7.0,
        now=time.time,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "memo.json"
        self.sessions_path = self.directory / "sessions.jsonl"
        self.retention_s = max(1, int(retention_days)) * 86400.0
        self.max_events = max(20, int(max_events))
        self.half_life_days = max(0.5, float(half_life_days))
        self._now = now
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=self.max_events)
        self._profile: dict[str, dict[str, Any]] = {}
        self._started_wall = _iso(self._now())
        self._started_mono = self._now()
        self._session: dict[str, Any] = {
            "started_wall": self._started_wall,
            "decisions": {"yes": 0, "no": 0, "unclear": 0},
            "ambient_opens": 0,
            "ambient_short_opens": 0,
            "ambient_veto_opens": 0,
            "corrections": 0,
            "environments": {},
            "events": 0,
        }
        self._last_saved_wall = ""
        self._last_prompt = ""
        self._last_scene = "未知"
        self._last_sample_at = 0.0
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ 读写

    def _load(self) -> None:
        """尽力恢复；坏文件不阻断宿主（下一次保存会覆盖它）。"""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        now = self._now()
        for event in data.get("events") or []:
            try:
                timestamp = float(event.get("t") or 0)
            except (TypeError, ValueError):
                continue
            if now - timestamp <= self.retention_s:
                self._events.append(dict(event))
        profile = data.get("profile")
        if isinstance(profile, dict):
            self._profile = {
                str(key): dict(value)
                for key, value in profile.items()
                if isinstance(value, dict)
            }
        self._last_saved_wall = str(data.get("saved_wall") or "")

    def _save(self) -> None:
        """原子写：先写临时文件再替换，进度中崩溃不会留下半个 JSON。"""
        with self._lock:
            payload = {
                "schema": SCHEMA,
                "saved_wall": _iso(self._now()),
                "retention_days": round(self.retention_s / 86400.0, 1),
                "events": list(self._events),
                "profile": self._profile,
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.path)
            self._last_saved_wall = payload["saved_wall"]
            self._dirty = False

    def save(self) -> None:
        with self._lock:
            self._save()

    # ------------------------------------------------------------------ 聚合

    def _entry(self, scene: str, band: str, now: float) -> dict[str, Any]:
        """取（或建）一个场景×时段仓位，并把旧计数按半衰期衰减。"""
        key = _key(scene, band)
        entry = self._profile.get(key)
        if entry is None:
            entry = {
                "scene": scene,
                "band": band,
                "updated": now,
                "decisions": {"yes": 0.0, "no": 0.0, "unclear": 0.0},
                "ambient": {"opens": 0.0, "short_opens": 0.0, "veto_opens": 0.0},
                "noise": {"samples": 0.0, "mean_dbfs": -60.0},
                "corrections": {"open_bad": 0.0, "open_ok": 0.0},
            }
            self._profile[key] = entry
            return entry
        updated = float(entry.get("updated") or now)
        days = max(0.0, (now - updated) / 86400.0)
        factor = 0.5 ** (days / self.half_life_days) if days > 0 else 1.0
        if factor < 0.999:
            for section in ("decisions", "ambient", "corrections"):
                values = entry.get(section) or {}
                for name, value in list(values.items()):
                    if isinstance(value, (int, float)):
                        values[name] = float(value) * factor
            noise = entry.get("noise") or {}
            if isinstance(noise.get("samples"), (int, float)):
                noise["samples"] = float(noise["samples"]) * factor
            entry["updated"] = now
        return entry

    def _append_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        self._session["events"] = int(self._session.get("events", 0)) + 1
        self._dirty = True

    # ------------------------------------------------------------------ 记录

    def record_decision(
        self,
        decision: dict[str, Any] | None,
        *,
        scene: str = "未知",
        environment: str = "",
        now: float | None = None,
    ) -> None:
        """记录一次对话判定（LLM 或本地回退）。由 assistant 的发布回调驱动。"""
        if not decision:
            return
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            dialogue = str(decision.get("dialogue") or "unclear")
            if dialogue not in ("yes", "no", "unclear"):
                dialogue = "unclear"
            event = {
                "t": round(timestamp, 3),
                "kind": "decision",
                "scene": scene,
                "environment": environment,
                "dialogue": dialogue,
                "ambient": "open" if decision.get("ambient") == "open" else "close",
                "confidence": decision.get("confidence"),
                "source": decision.get("source"),
                "reason": str(decision.get("reason") or "")[:120],
                "number": decision.get("number"),
            }
            self._append_event(event)
            entry = self._entry(scene, _band_label(timestamp), timestamp)
            entry["decisions"][dialogue] = float(entry["decisions"].get(dialogue, 0.0)) + 1.0
            self._session["decisions"][dialogue] += 1
            self._last_scene = scene
            self._save()

    def record_ambient(
        self,
        *,
        open: bool,
        scene: str = "未知",
        environment: str = "",
        intent: bool | None = None,
        authorization: dict[str, Any] | None = None,
        duration_s: float | None = None,
        now: float | None = None,
    ) -> None:
        """记录环境声的一次开/关。

        只在"意图/执行状态真的变了"时调用（调用方判转变），否则统计会被刷爆。
        `duration_s` 是上一次 open 到现在的时间，用来识别"开了马上关"的可疑开启。
        """
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            authorization = authorization or {}
            state = str(authorization.get("state") or "")
            veto = bool(authorization.get("veto")) or state in ("llm_veto", "local_veto")
            event = {
                "t": round(timestamp, 3),
                "kind": "ambient",
                "scene": scene,
                "environment": environment,
                "open": bool(open),
                "device_intent": intent,
                "state": state,
                "veto": veto,
                "duration_s": None if duration_s is None else round(float(duration_s), 1),
            }
            self._append_event(event)
            entry = self._entry(scene, _band_label(timestamp), timestamp)
            self._last_scene = scene
            ambient = entry["ambient"]
            if open:
                ambient["opens"] = float(ambient.get("opens", 0.0)) + 1.0
                self._session["ambient_opens"] += 1
            else:
                if duration_s is not None and duration_s < SHORT_OPEN_S:
                    ambient["short_opens"] = float(ambient.get("short_opens", 0.0)) + 1.0
                    self._session["ambient_short_opens"] += 1
                if veto:
                    ambient["veto_opens"] = float(ambient.get("veto_opens", 0.0)) + 1.0
                    self._session["ambient_veto_opens"] += 1
            self._save()

    def record_correction(
        self,
        *,
        verdict: str,
        scene: str = "未知",
        reason: str = "",
        now: float | None = None,
    ) -> None:
        """记录一次用户纠正（面板/接口将来接；先留数据入口）。

        verdict: `open_bad`（这次不该开环境声）/ `open_ok`（该开）。
        纠正是最贵的信号：它来自用户，而不是模型自己的判断。
        """
        if verdict not in ("open_bad", "open_ok"):
            raise ValueError("verdict 必须是 open_bad / open_ok")
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            self._append_event(
                {
                    "t": round(timestamp, 3),
                    "kind": "correction",
                    "scene": scene,
                    "verdict": verdict,
                    "reason": str(reason or "")[:120],
                }
            )
            entry = self._entry(scene, _band_label(timestamp), timestamp)
            entry["corrections"][verdict] = float(entry["corrections"].get(verdict, 0.0)) + 1.0
            self._session["corrections"] += 1
            self._last_scene = scene
            self._save()

    def sample_frame(
        self,
        *,
        scene: str = "未知",
        environment: str = "",
        broadband_dbfs: float | None = None,
        voice_likelihood: float | None = None,
        now: float | None = None,
    ) -> bool:
        """每 10 秒采一次环境统计（噪声均值 + 环境分布）。返回是否真的采样了。"""
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            if timestamp - self._last_sample_at < SAMPLE_INTERVAL_S:
                return False
            self._last_sample_at = timestamp
            if isinstance(broadband_dbfs, (int, float)):
                entry = self._entry(scene, _band_label(timestamp), timestamp)
                noise = entry.setdefault("noise", {"samples": 0.0, "mean_dbfs": -60.0})
                samples = float(noise.get("samples", 0.0))
                mean = float(noise.get("mean_dbfs", -60.0))
                noise["samples"] = samples + 1.0
                noise["mean_dbfs"] = (mean * samples + float(broadband_dbfs)) / (samples + 1.0)
            environments = self._session.setdefault("environments", {})
            if environment:
                environments[environment] = int(environments.get(environment, 0)) + 1
            self._dirty = True
            if self._dirty:
                self._save()
            return True

    # ------------------------------------------------------------------ 注入

    def prompt_context(self, *, scene: str = "未知", now: float | None = None) -> str:
        """给 LLM 的紧凑记忆摘要（≤300 字）。可能为空。

        它是**弱先验**：调用方（assistant 的 prompt）必须写明"冲突时以本段证据为准"。
        """
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            recent = [
                event
                for event in self._events
                if timestamp - float(event.get("t") or 0) <= 1800.0
            ]
            decisions = [event for event in recent if event.get("kind") == "decision"]
            closed = [
                event
                for event in recent
                if event.get("kind") == "ambient" and event.get("open") is False
            ]
            corrections = [
                event for event in recent if event.get("kind") == "correction"
            ]
            band = _band_label(timestamp)
            entry = self._profile.get(_key(scene, band)) or {}
            parts: list[str] = []
            if scene and scene != "未知":
                parts.append(f"场景 {scene}")

            if decisions:
                yes = sum(1 for event in decisions if event.get("dialogue") == "yes")
                no = sum(1 for event in decisions if event.get("dialogue") == "no")
                parts.append(f"近30分钟判定 {len(decisions)} 次（yes {yes}/no {no}）")
            if closed:
                suspicious = sum(
                    1
                    for event in closed
                    if event.get("veto") or (event.get("duration_s") or 99) < SHORT_OPEN_S
                )
                parts.append(f"环境声关 {len(closed)} 次、可疑 {suspicious} 次")
            if corrections:
                bad = sum(1 for event in corrections if event.get("verdict") == "open_bad")
                parts.append(f"用户纠正 {len(corrections)} 次（不该开 {bad}）")
            counts = entry.get("decisions") or {}
            total = sum(float(value) for value in counts.values() if isinstance(value, (int, float)))
            if total >= 3:
                ambient = entry.get("ambient") or {}
                parts.append(
                    f"{scene}·{band} 历史样本 {total:.0f}"
                    f"（对话 {float(counts.get('yes', 0.0)):.1f}、"
                    f"误开 {float(ambient.get('veto_opens', 0.0)):.1f}）"
                )
            elif not parts:
                parts.append("样本不足（<3），只作弱先验")
            text = "【用户记忆】" + "；".join(parts)
            self._last_prompt = text[:PROMPT_MAX_CHARS]
            return self._last_prompt

    # ------------------------------------------------------------------ 对外

    #: 影子建议的最小样本量。样本不够就不给建议——这比给一个看着像结论的猜测便宜。
    SHADOW_MIN_SAMPLES = 8

    def shadow_advice(self, *, scene: str | None = None, now: float | None = None) -> dict[str, Any]:
        """影子建议（D38 阶段 3 第 1 步）：**只给慢层与面板看，不执行**。

        规则是确定性的，不做机器学习；目的是把"记忆是否足够支撑一个动作"摆到台面上：
        - 样本 < 8：`insufficient`，任何方向都不给；
        - 环境声短开/被否决比例高：`ambient_bias = conservative`（下次别急着开）；
        - 对话判定里 yes 占比高且样本足够：`conversation_prior = likely`；
        - 否则 `neutral`。

        真正进入执行前，还要过 `--memo` 的样本量/一致率门槛与 D38 的受限执行约束。
        """
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            scene = scene or self._last_scene
            entry = self._profile.get(_key(scene, _band_label(timestamp))) or {}
            counts = entry.get("decisions") or {}
            total = sum(float(value) for value in counts.values() if isinstance(value, (int, float)))
            ambient = entry.get("ambient") or {}
            opens = float(ambient.get("opens", 0.0))
            short_opens = float(ambient.get("short_opens", 0.0))
            veto_opens = float(ambient.get("veto_opens", 0.0))
            yes = float(counts.get("yes", 0.0))
            if total < self.SHADOW_MIN_SAMPLES:
                return {
                    "state": "insufficient",
                    "scene": scene,
                    "samples": round(total, 1),
                    "min_samples": self.SHADOW_MIN_SAMPLES,
                    "note": "样本不足，不给方向（避免把猜测当结论）",
                }
            suspicious = short_opens + veto_opens
            ambient_bias = "conservative" if opens > 0 and suspicious / max(opens, 1.0) > 0.3 else "neutral"
            conversation_prior = "likely" if yes / total >= 0.6 else "neutral"
            return {
                "state": "shadow",
                "scene": scene,
                "samples": round(total, 1),
                "ambient_bias": ambient_bias,
                "conversation_prior": conversation_prior,
                "evidence": {
                    "opens": round(opens, 1),
                    "short_opens": round(short_opens, 1),
                    "veto_opens": round(veto_opens, 1),
                    "dialogue_yes": round(yes, 1),
                },
                "note": "影子建议：仅记录/展示，不进入执行（D38）",
            }

    def profile_entry(self, scene: str, *, now: float | None = None) -> dict[str, Any]:
        """取某个场景×时段仓位的当前计数（衰减到 now）。给影子建议与测试用。"""
        with self._lock:
            timestamp = self._now() if now is None else float(now)
            entry = self._profile.get(_key(scene, _band_label(timestamp)))
            return dict(entry) if entry else {}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "dir": str(self.directory),
                "events": len(self._events),
                "profile_keys": len(self._profile),
                "session": {
                    "decisions": dict(self._session.get("decisions") or {}),
                    "ambient_opens": self._session.get("ambient_opens", 0),
                    "ambient_short_opens": self._session.get("ambient_short_opens", 0),
                    "ambient_veto_opens": self._session.get("ambient_veto_opens", 0),
                    "corrections": self._session.get("corrections", 0),
                },
                "last_saved_wall": self._last_saved_wall,
                "prompt_preview": self._last_prompt[:120],
                "shadow_advice": self.shadow_advice(scene=self._last_scene),
            }

    def finalize_session(self) -> None:
        """运行结束时写一条结构化会话摘要（sessions.jsonl），然后落盘。"""
        with self._lock:
            payload = {
                "schema": SCHEMA,
                "type": "session_summary",
                "started_wall": self._session.get("started_wall"),
                "ended_wall": _iso(self._now()),
                "duration_s": round(max(0.0, self._now() - self._started_mono), 1),
                "decisions": dict(self._session.get("decisions") or {}),
                "ambient_opens": self._session.get("ambient_opens", 0),
                "ambient_short_opens": self._session.get("ambient_short_opens", 0),
                "ambient_veto_opens": self._session.get("ambient_veto_opens", 0),
                "corrections": self._session.get("corrections", 0),
                "environments": dict(self._session.get("environments") or {}),
                "events": self._session.get("events", 0),
            }
            try:
                with self.sessions_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except OSError:
                pass
            self._save()
