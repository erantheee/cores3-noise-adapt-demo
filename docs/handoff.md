# Session handoff — 2026-10-23

> 每轮收尾更新本文件（≤2.5KB）。新会话顺序：`AGENTS.md` → 本文件 → `docs/INDEX.md` → 按需读。
> 旧版交接（2026-10-22 明细，26KB）已归档到 `docs/handoff-archive-20261023.md`，不复读。

## 仓库状态
- 分支 `feature/apple-conversation-awareness`；本轮按"固件 / 上位机 / 文档"拆 3 个提交，工作树干净（HEAD 见 `git log`）；**远端未 push**（origin 仍停在 `66cdbd9`）。
- 测试口径（2026-10-23 实测）：`ctest` 12/12；host 自测 88 组 / 7914 条；渲染 80 条通过 / 0 失败；`sh tools/build.sh` exit 0。

## 本轮收尾内容（D41–D47）
- D41 背景人声改掩蔽；D42 环境声执行门；D43 ASR 可读性；D44 unknown 修复；D45 说话人聚合；D46 混合情境；D47 双麦取数（只遥测，已刷机，`mic2_*` 真机有效）。
- 端侧确认判据：连续 800 ms + 事件帧中立（W1 1179 帧零误触）。
- 上位机：voiceprint 在线学习、systemeq（QQ 音乐 tap）、memo、连续音频、训练数据。
- 收尾修复：仅 `--voiceprint` 启动时中继不再误报"已开启"（上报口径按 `--relay-audio` / `--ambient-executor relay|observe`）；render 断言与 systemeq 文案同步。

## 未完成 / 下一步
- **A0**：D40 真人对话验收（首响 0.5 s / 确认 0.8 s、误关漏关、噪声误触发）——需设备+真人。
- **A1**：LLM 迟到否决回写 S3（`dialog no|yes`）——代码未接。
- **#38**：真实房间本底被判语音未修；只能用设备 24 段 `bands[]` 取数。
- **D47**：近场/远场/安静三态数据未采完，判据阈值未定。
- 待拍板：6.5 dB 产品策略 vs 8.0 dB demo；D41 掩蔽无 limiter（现行口径已接受削波）。
- 声纹 self-veto 默认关、实验性（D31）。

## 当前环境（2026-10-23 15:30）
- CoreS3 插着 `/dev/cu.usbmodem101`；host 在跑（15:08 启动，默认三件套 + 训练数据，未开环境声执行端）；改完 host 代码需重启才生效。
- LLM = `gpt-5.6-sol`（`.env`），已抓到 `source=llm` 判定；ASR 本地 qwen3。
- 训练数据累计 1.7GB（gitignore）；声纹在线学习已启用。

## 验证入口
```bash
cmake -S test -B build-host-tests && cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
python3 tools/host/tests/test_host.py
node tools/host/tests/render_check.js <快照.json>
sh tools/build.sh            # 固件
```
