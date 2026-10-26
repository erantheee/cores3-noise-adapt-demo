# Session handoff — 2026-10-26

> 每轮收尾更新本文件（≤2.5KB）。新会话顺序：`AGENTS.md` → 本文件 → `docs/INDEX.md` → 按需读。
> 旧版交接（2026-10-22 明细）已归档到 `docs/handoff-archive-20261023.md`，不复读。

## 仓库状态
- 分支 `feature/apple-conversation-awareness`；工作树干净（HEAD 见 `git log`）；远端未 push（origin 仍停在 `66cdbd9`）。
- 测试口径（2026-10-26 实测）：`ctest` 12/12；host 自测 90 组 / 7928 条；渲染约 80/0；`sh tools/build.sh` exit 0。

## 本轮内容（D41–D47 + 演示控制）
- D41 掩蔽；D42 执行门；D43 ASR 可读性；D44 unknown 修复；D45 说话人聚合；D46 混合情境；D47 双麦取数（只遥测，已刷机，`mic2_*` 有效）。
- 端侧确认判据：连续 800 ms + 事件帧中立（W1 1179 帧零误触）。
- 上位机：voiceprint 在线学习、systemeq、memo、连续音频、训练数据。
- 2026-10-26 演示控制：`启动演示.command` / `停止演示.command` + 面板「演示控制」（暂停只停执行端，采集与 AI 继续；`/api/execution`）。S3 触摸按钮下一步（需刷机+补字库）。

## 未完成 / 下一步
- **A0**：D40 真人对话验收（首响 0.5 s / 确认 0.8 s、误关漏关、噪声误触发）——需设备+真人。
- **A1**：LLM 迟到否决回写 S3（`dialog no|yes`）——代码未接。
- **#38**：真实房间本底误判未修；设备麦数据显示四判据退化成 `vocal_ratio` 单判据（见 `work/diagnosis/room-floor-findings.md`）。
- **D47**：近场/远场/安静三态数据未采完，判据阈值未定。
- 待拍板：6.5 dB vs 8.0 dB；D41 掩蔽无 limiter。
- 声纹 self-veto 默认关、实验性（D31）。

## 当前环境（2026-10-26 10:46）
- host 在跑（默认三件套 + 训练数据）；S3 `/dev/cu.usbmodem101`（D47 固件）。
- 输出设备 Xiaomi 耳夹；LLM `gpt-5.6-sol`；ASR qwen3；声纹在线学习已启用；训练数据 1.7GB（gitignore）。

## 验证入口
```bash
cmake -S test -B build-host-tests && cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
python3 tools/host/tests/test_host.py
node tools/host/tests/render_check.js <快照.json>
sh tools/build.sh            # 固件
```
