# AGENTS.md — cores3-noise-adapt-demo

> 本仓库的 agent 入口（DSH 会自动注入）。**开工先读本文件 + `docs/handoff.md`**，再按需读其他文档。
> 目的：用 ~2KB 拿到"当前状态 / 口径 / 坑"，不必重读 300KB 的文档。

## 项目是什么
CoreS3 = AI OWS 耳机仓（端侧感知/决策）；Mac = 播放器与原型执行器；耳机 = OWS 耳夹（Xiaomi）或 TWS 对照（AirPods Pro），同版本自动识别（D35/D36）。
验证的是：预判式对话感知（0.5 s 预下压 70% / 0.8 s 确认 60%，D16/D40）、噪声自适应歌曲 EQ（D39）、背景人声掩蔽（D41）、环境声执行门（D42）。
**不做**：降噪、音频回传注入（D2/D3）；不宣称目标人声增强、双耳原始麦流、声学 DOA/波束（D36）。

## 当前状态（2026-10-23；详情以 `docs/handoff.md` 为准）
- 分支 `feature/apple-conversation-awareness`；本轮按"固件 / 上位机 / 文档"拆 3 个提交，工作树干净（HEAD 见 `git log`）；远端尚未 push（origin 仍停在 `66cdbd9`）。
- 最新：D41 背景人声改"掩蔽"；D42 `--ambient-gate off|llm|voice`（voice=演示默认）；D43 ASR 可读性；D44 unknown 修复；D45 说话人聚合；D46 混合情境；D47 双麦取数（只遥测，已刷机）。
- 待办：TODOS **A0**（D40 真人对话验收，需设备+人）、**A1**（LLM 迟到否决回写 S3，代码未接）；第 38 项"真实房间本底被判成语音"未修；D47 近场/远场/安静三态数据未采完、阈值未定；声纹 self-veto 默认关、实验性（D31）。

## 关键文件（按需读，不要整篇扫）
- `main/conversation_awareness.{hpp,cpp}` — 预判对话状态机（D16/D40）
- `main/controller.{hpp,cpp}` — 策略/增益：`kMaxSpeechGainDb=6.5`（产品策略）、`kDemoSpeechGainDb=8.0`（demo 实发）
- `main/app_main.cpp` — 主循环 / 串口协议 v4；`main/demo_ui.cpp` — 屏幕
- `main/{sound_event.hpp,listening_model.h,voiceprint_gate.*,bone_voice.*}` — 端侧规则/模型/证据
- `tools/host/` — 上位机（`__main__.py` 入口，`assistant.py` ASR/LLM，`state.py` 快照，`memo.py` 账本，`web/` 面板）；`tools/player/` — Swift 实时 24 段 EQ
- `test/*_test.cpp`（ctest）、`tools/host/tests/test_host.py`、`tools/host/tests/render_check.js`
- `tools/{build.sh,flash.sh,monitor.sh,host_up.sh,free_port.sh,record_from_device.py}`
- 索引：`docs/INDEX.md`（先看，含归档级标记与已知矛盾）；口径：`docs/product-alignment.md`；状态：`docs/plan-status.md`（只看状态表/下一步）；决策：`docs/decisions.md` 按 D 编号 grep（最新 D47）

## 构建 / 测试（改完先跑）
```bash
# 主机 C++ 单测（秒级，无需设备）
cmake -S test -B build-host-tests && cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
# 上位机自测（秒级）
python3 tools/host/tests/test_host.py
node tools/host/tests/render_check.js <快照.json>   # 可选：DOM 桩渲染
# 固件：本机已装 ESP-IDF v5.4.1（~/esp/esp-idf + python3.12 env）
sh tools/build.sh             # 只编译
sh tools/build.sh flash       # 编译后刷写（复用 tools/flash.sh）
# 备用：CI 构建（push 触发，gh run download 产物后 sh tools/flash.sh <产物目录>）
```
- 刷写前 `sh tools/free_port.sh --kill` 释放串口；`tools/flash.sh` 逐区写+校验（"Hash of data verified" ≠ 刷写成功）。
- 上位机：Finder 双击 `启动演示.command`（自动 free_port + 默认三件套）或在终端 `sh tools/host_up.sh --detach`（面板 http://127.0.0.1:8787/）；无设备加 `--mock`。
- 运行中控制：面板顶部「演示控制」= 暂停/继续/关闭（暂停只停执行端，采集与 AI 继续）；关闭也可双击 `停止演示.command`。S3 触摸按钮尚未做（需刷机+补字库）。
- 断言/测试条数以**实际运行输出**为准（文档里的数字会漂移）。

## 硬规则 / 已知坑
- **多会话协作（2026-10-23）：host 归谁管**。同一台机器的多个 DSH 会话共享这个仓库和串口。
  ① host 只能经 `sh tools/host_up.sh --detach` 启动——脚本已**固化演示默认三件套**
  （`--system-eq-app com.tencent.QQMusicMac --eq-mode basic --ambient-gate voice --voiceprint`），
  任何会话裸跑都会带上 EQ，不会再把 systemeq 冲掉；要额外功能显式加参
  （如对方会话的中继：`sh tools/host_up.sh --detach --relay-audio --ambient-executor relay`）。
  ② 重启 host 前先 `curl -s http://127.0.0.1:8787/api/snapshot` 确认现状：systemeq 已在就别重启；
  确实要重启，先 `sh tools/free_port.sh --kill` 再 host_up，**禁止 kill 对家进程后不管**。
  ③ 两个会话需求冲突时，把参数合并进同一条启动命令（功能可共存），不要互相 kill 抢串口。
- 口径冲突：`docs/product-alignment.md` + `docs/decisions.md` 为准；README 是索引/变更叙事，不是权威。文档常落后于代码——**先 `git status` / `git diff`，别把文档数字当结论**。
- 掩蔽 ≠ 降噪 ≠ 人声增强；不做降噪执行端；不宣称波束/DOA/双耳麦（D36）。
- 声纹：低相似度 = `unknown`，不是 `other`；pending/过期/断连必须 fail-closed（D31）；self 不回传 S3。
- 串口：一个端口只允许一个 reader；USB-Serial/JTAG 必须 DTR=True；采集/刷机前先 free_port。
- 固件不报版本/构建 SHA（只报 `schema:4`）：真机数字只能靠新增键（如 `mic2_*` = D47 之后）推断固件版本；核对靠构建时间/刷机记录（TODOS P2「固件版本上报」待补）。
- 设备录音只到 16 kHz（中继带宽上限），不能拿它验证 24 频段判据；真机数字必须带固件版本。
- 面板按 6.5 dB 校验；8.0 dB 帧不要标成"超出硬上限"。
- 改行为补 C++ 单测（或说明为何不需要）；CI 顺序：host-tests → firmware。

## 会话协议（防"越聊越笨"）
1. 开工先读 `docs/handoff.md`（≤2.5KB，记录上一轮状态/下一步），不要重读大文档。
2. 任务收尾（或上下文将满）时**更新 `docs/handoff.md`**：日期、分支/HEAD、做了什么、验证结果、未完成项、下一步命令。
3. 长会话超过约 400–500K tokens 或触发压缩后，先写 handoff 再新开会话；不要在一个会话里连续跨多天/多特性。
4. 大文档只 grep/分段读：`grep -n '^## D' docs/decisions.md`、`sed -n '41,172p' docs/plan-status.md`。
5. 若本会话被压缩（compaction）：先重读 `docs/handoff.md` + `docs/product-alignment.md` 再继续；不确定用户要求时先复述确认，不要凭摘要猜。
