# TODOS（2026-10-21 收尾遗留）

> 本轮（文档全量同步 + 仓库卫生 + 2026-10-21 现场反馈修复）明确**不做**的代码/流程事项。
> 每条含 **What / Why / Cons / Context / 依赖**。按建议优先级排序；完成一条就删掉或勾掉。
>
> 已完成（不在本文件）：文档与代码对齐、`.gitignore` 音频策略、根 `LICENSE`（MIT）、
> 声纹 self-veto 的过期/clear/遥测、上位机回控回执与 `/api/stop` 清理、ASR 噪声门控与
> 去幻觉（D33）、LLM 判定作为环境声执行授权（D32）、噪声自适应歌曲 EQ 重做 + 有符号协议
> （D39）、对话确认改为持续证据（D40）。见 `docs/plan-status.md` 第 72/74/75/80 项。

## 现场反馈修复的后续（2026-10-21 深夜新增，优先于下列 P1）

### A0. D40 真人对话验收：首响延迟、误关/漏关、背景噪声误判
- **What**：真机跑 3 类场景各 ≥3 次——真实两人对话 / 电视播客单向人声 / 嘈杂背景人声无对话，
  记录：首次预下压延迟（设计值 0.5 s）、确认延迟（0.8 s）、对话中被误释放次数、
  背景噪声被误判成对话的次数与时长、ASR/LLM 迟到否决是否与端侧判定一致。判据先写死再跑。
- **Why**：D40 把首响从 0.2–0.3 s 改成 0.5/0.8 s 是有意的取舍（现场噪声连续段最长 640 ms），
  但没有真人数据就不能证明"真人对话仍来得及、噪声不再误触发"。
- **Cons**：需要真人 + 现场 + 串口；"连续证据 800 ms"可能对语速慢/停顿多的人偏保守。
- **Context**：`main/conversation_awareness.{hpp,cpp}`、`work/diagnosis/state.md`、
  `test/conversation_awareness_test.cpp`、D40。
- **依赖**：设备 + 真人。

### A1. LLM 迟到否决回写 S3 快路径（补全 D34 闭环）
- **What**：默认启动配置下 host 不登记 ASR 采集（mic 块在流但 assistant 丢弃）；且
  host→S3 只有 route/rec/voiceprint 控制行，没有把 `dialogue=no` 回写快路径的通道。
  需：① 加 `--assistant-capture`（或首个 `MicChunk` 补登记）让 ASR/LLM 在默认 demo 也能跑；
  ② 新增 S3 控制行 `dialog no|yes [ttl]`，host 在 `llm_veto`/`local_veto` 时下发，S3 在 TTL
  内强制释放对话态（允许 SONG_EQ/EXTERNAL_VOICE_INTENT、关闭环境声）。
- **Why**：D40 只用本地证据持续性做第一时间区隔，无法处理"连续电视/播客"（连续证据长）。
  LLM 语义否决是 D34 声明的第二道防线，现在实际没有接线。
- **Cons**：串口音频流量 ~43 kB/s（base64），要复测 latency p50/p95；控制行与 TTL 要单测；
  ASR/LLM 在默认 demo 会常驻占用 Mac 算力。
- **Context**：`tools/host/__main__.py`（capture 登记、ambient 授权消费）、
  `tools/host/assistant.py`、`main/app_main.cpp`（控制行解析）、D31/D34/D40。
- **依赖**：设备 + 串口。

## 现场反馈修复的后续（2026-10-21 新增，优先于下列 P1）

### A. D34 快路径验收：误开时长、否决延迟、一致率
- **What**：真机跑 3 类场景各 ≥3 次（真实两人对话 / 电视播客单向人声 / 房间噪声无对话），
  记录：首次响应延迟（目标 ≤300 ms）、LLM/本地否决延迟、误开时长（开→被关）、漏开次数、
  两边判定一致率。判据先写死再跑。
- **Why**：D34 明确接受"快路径可能误开、之后被纠正"，但没有这三个数字就不能证明
  "快 + 可纠正"的 demo 价值；也不能对外宣称"对话判定可用"。
- **Cons**：需要真人 + 耳机/中继 + 串口独占；误开时长可能很长（ASR/LLM 变慢时）。
- **Context**：`tools/host/assistant.py` 的 `ambient_authorization`、`tools/host/training.py`
  的 `events.jsonl`（已记录 open/授权状态/判定年龄）、D34。
- **依赖**：设备 + 中继 + 真人。

### B. 训练数据的标注与质量门槛
- **What**：目前 `labels.teacher` 由 ASR/LLM 自动生成、`labels.human` 为空。需要：
  ① 写 `tools/label_training_data.py`（按场景抽样、人只改教师标签）；② 定义训练集/验证集
  切分与质量门槛（至少覆盖独处/媒体/两人对话/多人背景）；③ 隐私与保留策略（默认落
  `记录/训练数据/`，gitignore，单会话 2 GB 上限）。
- **Why**：没有人工校验的教师标签会把 ASR/LLM 的错误蒸馏进端侧模型；D34 的 C 路线前提是
  数据可信。
- **Cons**：需要人听音频标注，成本高；长会话存储增长快。
- **Context**：`tools/host/training.py`、D34 第 4 条；现有 `记录/教师数据`、`--frames-jsonl`。

### C. 离线蒸馏：把 LLM 语义编译进端侧快路径（D34 的下一阶段）
- **What**：用训练数据训端侧对话预判/语义小模型（特征：遥测 + 声纹轮次 + ASR 文本或
  文本嵌入；标签：教师 dialogue/ambient），替换/增强规则打分；LLM 留在环外做教师/复盘。
- **Why**：这是"既有 LLM 语义又零延迟"的唯一路径；现场 API 物理上做不到 <300 ms。
- **Cons**：需要素材量与训练脚本；端侧内存/算力预算要重算；可能过拟合现场房间。
- **Context**：`docs/conversation-prediction.md`、`tools/host/training.py`、D34。
- **依赖**：B 的标注数据。

### D. memo 影子建议与纠正入口（D38 阶段 3）
- **What**：profile 先生成影子建议（只记录不执行）；面板/API 接用户纠正入口
  （`open_bad` / `open_ok`，`MemoStore.record_correction()` 已留好）；达到样本量与一致率
  门槛后，才允许作为慢层先验进入执行（EQ 偏置 ≤2 dB、带 TTL、`--no-memo` 可一键关闭）。
- **Why**：D38 的"学用户"必须可验收；没有纠正信号和影子期，记忆只是一份日志。
- **Cons**：需要真人多场景数据；一旦动执行会牵动 D0 的"谁在做决策"口径，需单独立决策。
- **Context**：`tools/host/memo.py`、D38；`profile` 已按场景×时段落盘。
- **依赖**：B 的标注流程；面板新增两个纠正按钮。

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
- **Cons**：需真机 + 耳机/relay 环境。
- **Context**：`tools/host/__main__.py` ambient 循环、`RelayAmbient`。
- **依赖**：设备 + 耳机。

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

### 15. 固件版本 / 构建 SHA 上报
- **What**：固件在启动横幅或首帧上报 `git describe`/构建 SHA（可选用追加键，不涨 schema），host 面板显示。
- **Why**：真机数字必须带固件版本。现在固件只报 `schema:4`，只能靠"有没有 `mic2_*` 键"推断是 D47 之后，无法核对设备跑的是哪个 commit、也无法自证 CI 产物与本地构建一致。
- **Cons**：要动固件并重刷；构建时注入版本需要改 CMake/IDF 配置（例如 `git rev-parse --short HEAD` 写宏）。
- **Context**：`main/app_main.cpp` 启动与首帧；`tools/host/protocol.py` 可选键解析；`docs/plan-status.md`「已知的维护风险」；2026-10-23 审计发现。
- **依赖**：下次刷机窗口。
