# TODOS（2026-10-21 收尾遗留）

> 本轮（文档全量同步 + 仓库卫生 + 2026-10-21 现场反馈修复）明确**不做**的代码/流程事项。
> 每条含 **What / Why / Cons / Context / 依赖**。按建议优先级排序；完成一条就删掉或勾掉。
>
> 已完成（不在本文件）：文档与代码对齐、`.gitignore` 音频策略、根 `LICENSE`（MIT）、
> 声纹 self-veto 的过期/clear/遥测、上位机回控回执与 `/api/stop` 清理、ASR 噪声门控与
> 去幻觉（D33）、LLM 判定作为环境声执行授权（D32）。见 `docs/plan-status.md` 第 72/74/75 项。

## 现场反馈修复的后续（2026-10-21 新增，优先于下列 P1）

### A. 真机多说话人对话验收（D32 的开放条件）
- **What**：用真机跑 3 类场景各 ≥3 次：真实两人对话（self+other 都出现）、电视/播客单向人声、
  房间噪声无对话；记录"对话开始 → LLM 授权 → 环境声真正打开"的延迟、误开/漏开次数。
- **Why**：当前只有离线 API 实测（8 次结构化输出 8/8、p50 5.10 s）与单测；**没有**真机语义
  对话验收，"LLM 决定环境声开不开"还不能对外当成产品结论。
- **Cons**：需要有人在场说话、AirPods/中继可用、串口独占；可能暴露 VAD 门限与 6 s 分段的
  新问题。验收前不得宣传"对话判定已可用"。
- **Context**：`tools/host/assistant.py`（`_select_speech_regions` / `_summarize_event`）、
  `tools/host/__main__.py` 环境声循环、D32/D33。
- **依赖**：设备 + AirPods + 真人；判据先写死（漏开/误开/延迟门槛）再跑。

### B. ASR 远场准确率与语种混杂
- **What**：当前只保证"噪声不写成文字"；远场 16 kHz 单麦转写准确率、英文/中英混杂、多人
  重叠语音都未验收。下一步用真机录音做小规模 WER 抽查，再决定是否换模型/加热词/双麦。
- **Why**：D33 的去幻觉过滤能挡住重复输出，但挡不住"每个字都像中文、整句是错的"。
- **Cons**：需要素材与人工标注；换模型会改变 `.asr-venv` 体积与启动时间。
- **Context**：`tools/qwen_asr_worker.py`、`记录/` 现有素材；D33 边界。
- **依赖**：真机录音（`tools/record_from_device.py` + 中继）。

### C. LLM 成本/限流/超时策略
- **What**：6 s 一段、连续候选环境下约 10 次 LLM 调用/分钟。需要决定：连续对话时是否降低
  判定频率、是否换更便宜模型、失败重试与超时（当前 45 s）、月度成本上限。
- **Why**：现场连续 LIKELY 时调用量比 15 s 分段高 2.5 倍；没有限流会在长时间演示里烧钱。
- **Cons**：降低频率会拉长授权续期窗口，需要与 TTL（20 s）一起调；实测数据不足。
- **Context**：`AssistantService._MAX_CONTINUOUS_SEGMENT_S` / `_DECISION_TTL_S`、D32。
- **依赖**：先有 A 的真实调用量数据。

## P1（建议下一步）

### 1. 固件 P2 小修批次：`no_data` 判定、控制任务忙等
- **What**：固件里已知的 P2 级小问题（`no_data` 判定/上报口径、控制任务忙等）本轮不动。
- **Why**：都不阻塞 demo，但会在日志与延迟读数里制造噪声。
- **Cons**：改固件要重刷并重测延迟；可能牵动控制时序。
- **Context**：`main/app_main.cpp` 控制任务；本轮收尾的固件只动声纹门控，这些 P2 未动。
- **依赖**：等行为定稿后与其它固件改动一起做。

### 2. EQ 平滑跨模式
- **What**：`eq_gain_db_` 的平滑状态在 `tuning_mode` 切换时从旧模式值继续收敛，切换瞬间可能
  拖尾或短暂越界。
- **Why**：面板曲线与实际输出必须一致；听感上可能听到短暂抽动。
- **Cons**：需要新增跨模式回归，可能改动单测金标准。
- **Context**：`main/controller.cpp` 的 EQ 平滑段（上升 650 ms / 回落 850 ms）；HEAD `ad77572`。
- **依赖**：与 controller 行为定稿一起改。

### 3. 控制任务忙等与 IMU 30 Hz 根因
- **What**：IMU 实测 29.2–30.1 Hz（设计 100 Hz，被动检查 C4c 不通过）；控制任务忙等只是候选
  原因之一。
- **Why**：影响时序类实验与文档口径；骨传导判据虽已被否定，但采样率问题仍会误导后人。
- **Cons**：需独占串口看真机日志；可能改任务优先级/队列排空方式，牵动音频任务。
- **Context**：`tools/check_device_passive.py`、`docs/apple-conversation-awareness.md` 第 16.5 节。
- **依赖**：设备在场。

### 4. verify C1–C9 按 D16 重写
- **What**：`tools/verify_conversation_awareness.py` 仍是 `own_voice` 时代的断言（C1 认 v3、
  C9 要求旁人说话不触发），与 D16 预判式冲突。
- **Why**：真机验收工具不可用，跑出来的报告不能用来决策。
- **Cons**：要改五相脚本与断言，可能需重烧固件。
- **Context**：`docs/apple-conversation-awareness.md` 第 17.4 节、plan-status 第 41 项。
- **依赖**：固件行为稳定后。

### 5. 语音 gate 真数据（D19）
- **What**：二分类 speech gate 需真实多说话人/多距离数据后重训；当前跨会话误报 3–44% 未过线。
- **Why**：过了 D19 准入才允许替换四判据。
- **Cons**：采集与人工标注成本高。
- **Context**：plan-status 第 50/53/59 项、D19。
- **依赖**：真实素材。

### 6. v4 真机夹具重抓
- **What**：`tools/host/tests/fixtures/device_real.log` 仍是 schema 2（2026-10-15），当前固件
  已是 schema 4。
- **Why**：v4 真帧（`ambient_open` / `event` / `noise_spectrum` / 声纹回显）没有真机日志回归。
- **Cons**：需设备；换夹具要同步测试期望。
- **Context**：README「主机单元测试」一节。
- **依赖**：固件定稿。

## P2

### 7. 上位机面板 XSS/CSRF 审查
- **What**：`tools/host/web` 对设备/用户输入的回显转义与本地接口的鉴权未做专项审计。
- **Why**：面板会展示设备原始日志，并可通过白名单发录音回控。
- **Cons**：本地 demo 风险低，改动在 web/server 层。
- **Context**：`tools/host/server.py`（只监听本地）、`tools/host/web/index.html`。
- **依赖**：无。

### 8. 协议健壮性：NaN/Inf、`context_score`、行上限
- **What**：解析层对 NaN/Inf、缺失 `context_score`、超长行的防御与单测。
- **Why**：坏帧不应污染状态或让面板显示错数。
- **Cons**：补 parser 单测，可能改变容错行为。
- **Context**：`tools/host/protocol.py`、`tools/host/tests/test_host.py`。
- **依赖**：与上位机改动协调。

### 9. ambient 退出竞态与 160 ms 预缓冲
- **What**：`/api/stop` 已改为先清理（含 `ambient.stop`）再退出；但 ambient 执行端在退出/重连
  时的竞态、160 ms 预缓冲是否该保留，仍待压测。
- **Why**：退出时不留残音、不漏关。
- **Cons**：需真机 + AirPods/relay 环境。
- **Context**：`tools/host/__main__.py` ambient 循环、`RelayAmbient`。
- **依赖**：设备 + AirPods。

### 10. `check_device_passive` CLI 化
- **What**：`tools/check_device_passive.py` 的参数化（`--seconds` / `--strict` / 退出码）与
  CI 之外的入口未做。
- **Why**：现场复测可脚本化、成本低。
- **Cons**：需面板/设备在跑。
- **Context**：`tools/check_device_passive.py`、apple 文档第 16.5 节。
- **依赖**：无。

### 11. `dependencies.lock` 提交后的复现说明
- **What**：lock 由 Lead 提交后，补 README/CI 的"如何用 lock 复现固定组件版本"说明。
- **Why**：同一 commit 的固件应可复现，CI 不应随组件新版漂移。
- **Cons**：需确认 CI 使用 lock。
- **Context**：`main/idf_component.yml`（`^3.0.0`）、`.gitignore`（已删除 lock 忽略行）。
- **依赖**：Lead 提交 lock。

### 12. 端侧目标人声提取（TSE）
- **What**：6 GMACs 量级，另一个工程，不在本轮范围。
- **Why**：产品目标人声增强的前提。
- **Cons**：需要模型/算力/新的硬件假设。
- **Context**：D10、plan-status 第 14 项。
- **依赖**：无。

### 13. LE Audio 直连评估
- **What**：当前不追 LE Audio 直连（D3）；将来评估需按 D3 的触发条件重新测宿主与链路。
- **Why**：换链路会改变 211–231 ms 延迟的前提。
- **Cons**：等于另一个工程。
- **Context**：D2/D3。
- **依赖**：用户决定。

### 14. 公开音频历史重写（若将来要撤）
- **What**：若将来要撤下已公开的 `audio.wav`，需要 `git filter-repo` 重写历史 + 强制推送。
- **Why**：隐私或授权口径变化；删文件无法撤回已发布内容。
- **Cons**：重写所有 commit hash，协作者需要重新克隆。
- **Context**：`experiments/sound-events/data/README.md` 的「保留策略」、`.gitignore`。
- **依赖**：用户决定。
