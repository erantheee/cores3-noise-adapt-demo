# Session handoff — 2026-10-28（上午）

> 每轮收尾更新本文件（≤2.5KB）。新会话顺序：`AGENTS.md` → 本文件 → `docs/INDEX.md` → 按需读。
> 旧版交接已归档 `docs/handoff-archive-20261023.md`。

## 最新（2026-10-30 11:3x · 可听调音档真机达标 + B1/B2 落地 + A-1 止损）
- **阶段一（可听调音）**：真机 60 s / 563 帧（`记录/eq验收/20261030-live-1.csv`）：
  掩蔽矩阵全程启用、P50 17/24 段；`masking_peak_db` P50 2.22 dB；**applied 正向峰值
  P50 2.14 / P90 2.22 / max 2.29 dB，≥2 dB 占 85%**（改动前 1.61–1.95）→ 用户"至少 2 dB"
  首次在真机达成；窗口内 `out_clip_frames` 增量 0（启动期累计 13 帧：源峰值 1.0033 已越界，
  之后不增长）；余量中位 −2.93 dB；丢帧 0、执行错误 0。见 D56。
  **听感待用户确认**；M4（只有歌→矩阵应静默）当前房间有本底，待 B2 的 observe/apply A/B。
- **阶段二/B1 `music on|off`（已实现、已刷机、真机验证）**：host 在执行端实测在放歌时
  每 1 s 续期，设备 TTL 3 s fail-open；帧键 `music_playing`。真机验证：播放器播放 →
  快照 `control.last=music on（S3 记录「歌是 Mac 放的」）`、帧 true；暂停 → off、帧 false。
  设备侧 **只遥测**，对话/掩蔽判据未改。见 D57。
- **阶段二/B2 歌曲漏音残差（已实现、只观测）**：`tools/host/leak.py` 纯函数（谱形中位偏移、
  功率域相减、单段 ≤12 dB、离散度/置信/平谱三道门），快照 `leak` + 面板显示；
  `--leak-residual observe|apply|off`，**默认 observe 不改变调音**。真机 A/B 未做
  （需要 QQ 音乐在播才有实时内容谱；本轮 QQ 音乐静音，本地播放器出声时 systemeq 无内容谱）。见 D58。
- **A-1 止损**：面板情境卡逐字写「实验模型／未通过真机验收」，render_check 加断言；
  九分类仍只遥测，采集/重训未开始。
- **面板三档语义（2026-10-30 12:0x 用户拍板）**：`基础` = 掩蔽剂量 1.0×（约 2.15 dB），
  `激进` = 1.4×（约 3 dB，更明显），`关闭EQ` = 执行端 bypass（原声直通）。**不要新增按钮**
  （用户明确要求，刚加的「掩蔽 A/B」已删除）。剂量只放大逐段目标与段别 budget，安全门全保留；
  快照 `masking_dose`（执行端回报）/`masking_dose_sent`（host 实发）分列。真机 basic→1.0 /
  aggressive→1.4 已验证；听感验收现在用「基础 vs 激进 vs 关闭EQ」三档。
- **面板「当前媒体」卡（2026-10-30 12:1x，用户口径）**：原「播放（Mac · 原版原曲）」卡改为
  「当前媒体（听到的）」——媒体源（只认执行端，QQ 音乐/本机播放器/无）、曲目（QQ 音乐**读不到**，
  如实写不可读）、内容类型（**估计**，谱形+瞬态+高频密度的粗分类，标签带"（估计）"）、
  调音思路（哪几组频段、抬几段、峰值、剂量、瞬态门、余量、削波、漏音残差）。本地播放器控件
  收进 details。判断在 `tools/host/media.py`。host 106 组 / 8073 断言、render 72/72。见 D59。
- **掩蔽改清晰度倾斜（2026-10-30 12:4x，用户"完全没效果 + 声音还变小了"）**：根因是旧版
  抬 15 段×2 dB ≈ 整谱一起抬（听不出差别），且"预测峰值"把存在感区抬升误当峰值抬升，
  整条链被压低 4 dB。现改为：存在感区（1–4 kHz）+3 dB、低频（0–7 段）−2.5 dB 让位、
  高频 ≤1.5 dB；预测峰值按内容谱形加权；全局余量最多 −1 dB；trim 只在明显削波时收缩。
  实测 applied P50 2.06 / P90 2.77 dB、全局余量中位 −0.75 dB、削波 0.01%（源满量程）。
  **待用户听感**；若还不够，下一步是"降 QQ 音乐输出音量换 headroom 再抬响度"。
- 工具：`free_port.sh` 自动发现串口（不再写死 `usbmodem1101`）；`test_launcher.sh` 接入 CI；
  `live_band_capture.py` 新增 systemeq 列与分阶段摘要。
- 回归（实测）：host **104 组 / 8049 断言**、ctest **13/13**、render **68/68**、launcher 通过；
  固件已刷（含 `music_playing` 键）。
- 下一步：① 用户听感（阶段一 Gate）；② 恢复 QQ 音乐后做 B2 的 observe vs apply A/B；
  ③ B3 内容角色侧链；④ C1 采集工具读真实 `broadband_dbfs`；⑤ C2 九分类真机采集（需用户素材/在场）。

## 最新（2026-10-29 · S3 24 段实时掩蔽矩阵）
- **2026-10-30 断流保护**：修复“串口打开但 S3 已无数据，页面仍写已连接且 Mac 无限沿用
  最后一帧处方”。现在 1 s 无新控制帧即把 EQ 复位为平直、媒体 100%、关闭环境声；页面显示
  「S3 暂无新数据 / 数据中断」「已旁路」。真机复现为残留 `/dev/cu.usbmodem101` 可打开但
  10 s 零字节，host 安全退出；重新插入后真机恢复到 `control_data_live=true`、帧龄
  13–106 ms、Xiaomi OWS 路由、masking matrix 生效，15 s 无丢帧/削波/执行错误。回归
  **102 组 / 8004 断言**，断流渲染 **87/87**、恢复态渲染 **85/85**。
- **2026-10-30 可听调音档**：用户确认原矩阵“完全没感觉”；真机证据是在 -28.3 dBFS 高噪声
  下实际仅 +0.13 dB。现改为低/中/高任一频段只要遮蔽证据成立，都进入约 2 dB 目标，
  不再只加强中频；后级峰值目标收紧到约 -0.9 dBFS。真机 18 s 观察最大实际
  +1.61～1.95 dB、16～21 段参与、`out_clip_frames=0`、无执行错误，输出 Xiaomi OWS。
- 用户现场确认 Demucs 2 s 窗造成歌词严重不同步；进一步测到单窗 **2.736 s**，
  hop 1.5 s，RTF **1.368**，输出 ring 读空、输入 ring 塞满，严重断续。
- 主链现在是 **S3 24 段环境频谱 × QQ 音乐 24 段内容频谱**：只对“噪声占优且音乐不足”
  的重叠频段做有上限的补偿，不再将风噪/键盘分类直接映射成整首歌的固定 EQ；无有效
  频谱时 fail-open 到原音，旧固定策略只作旧固件兼容兜底。非分轨路径 `base_gain=0`。
- Mac 保留高频密度/瞬态风险门、段别增益上限和 EQ 后峰值余量；六分轨默认关闭，仅显式
  `--stem-remix` 进实验路径。
- 验证：host **101 组 / 7995 断言**，实时页面 **86/86**；真机 `masking_matrix_enabled=true`，
  现场 24 s 内激活 0–4/24 段、峰值约 +0.05–0.08 dB，ring 稳定、无执行错误。启动初期累计
  1 个削波采样，余量随后收紧且未继续增长。
- 界限：已经会判断“当前环境遮住了歌里哪些频段”，但还不识别人声/吉他/鼓等语义角色；
  下一层应是不进音频主链的轻量内容角色侧链，不能把 Demucs 延迟重新带回来。

## 最新补充（2026-10-28 19:0x · 声纹原型漂移事故与修复，仅 host 侧）
- 现象：用户报「自己说话被当成别人」。定位到在线自学习把原型学坏：`记录/声纹/self-device/
  learned-prototype.npz` 与 10-26 注册原型的 cosine 从 0.9885（updates 56）/ 0.9368（156）
  掉到 **0.3956**（updates 1120，18:46）；与 impostor-live 素材 mean 0.754 / max 0.927。
  自己说话 score≈0.40 → 连续 2 窗 ≤0.50（0.80−0.30）判 other。**不是 WavLM 的问题**：
  当前 3 段注册集止损门 12.63 / EER 0% 通过（`tools/voice_separability.py`）。
- 根因：worker 的 8% EMA 无锚点（时间常数 ~12 窗）；学习门槛 0.80 与判定阈值同值；
  host 侧透传不了 `--no-learn`；`host_up.sh` 默认自动加 `--voiceprint` → 一直开着学。
- 修复（未刷机）：① worker 加注册锚点 `--anchor-min-cos 0.90`：加载时离锚点过远 →
  拒绝 + rename 留档 + 冻结；线上更新越界 → 回退锚点并冻结；② learn_weight 0.08→0.01、
  learn_min_score 0.80→0.90；③ host 新增 `--voiceprint-learn`，**默认不学**；
  ④ 快照加 `learn_enabled/learn_frozen/learn_rejected/drift` 遥测。
- 验证（实测）：host **99 组 / 7978 断言** exit 0；重启后 worker 自动 reject 坏原型
  （`learn_reject drift=0.4768` → `learned-rejected-20261028-190049.npz`），ready
  `drift=1.0 / updates=0 / clips=3`；真机现场连续判 self（score 0.79–0.97），frame
  `mode=SONG_EQ / conversation=NONE / media=100`（不再因 other 触发对话）。
  坏原型留档 `/tmp/learned-broken-20261028-1846.npz`。
- 边界：`--ambient-gate voice` 下 self **故意不回传**设备（D42），所以设备
  `voiceprint_decision.valid=false` 是设计不是故障；该模式下设备侧 self-veto 不生效，
  自己说话仍可能被 D16 预判压 EQ（靠 D50 dialog veto 兜底），不是声纹能解决的。
- 未做：重新注册声纹（当前 3 段单一条件，类内抖动 0.028 说明条件覆盖极窄，应按
  `tools/record_voiceprints.py` 三档 + 分两天录）；`tools/free_port.sh` 串口编号匹配 bug
  （找 `usbmodem1101`，实际 `usbmodem101`，本次用 lsof+kill 手动绕过）。

## 最新补充（2026-10-28 17:4x · 环境声不开：上下文被清 + unknown 被当 unclear，D55）
- 用户报「环境声还是没有开啊」。查当天 17:07–17:36 的 132 段：LLM `dialogue=yes` **0 次**
  （no 44 / unclear 88），S3 只在 17:29:12 发过 35 帧 `ambient_open` 意图；且 host 是
  17:04「启动演示.command」起的默认 **`--ambient-executor off`** → 意图再真也不出声。
- 根因 1（bug）：96/132 段以"连续静音3秒"结束，`observe_frame` 推进授权 epoch 时把
  `_llm_context` 清空 → LLM 每段只看到一句 unknown，永远拼不出交替/延续。
  修：静音切轮次**只推进 epoch，不清语义上下文**；条目带 `at`，`_CONTEXT_TTL_S=120s`
  淘汰；给 LLM 的 `_context_facts()` 输出 `age_s`，prompt 写清 age_s 用法。
- 根因 2（prompt 自相矛盾）：第 3) 条允许 unknown 按语义判 yes，但"判 unclear：身份不足"
  把它盖掉；当天 98 段转写身份**全 unknown**（声纹 0.35–0.59 vs 0.8）。修：明确
  "identity_available=false 本身不是 unclear 的理由"，全 unknown 语义像多人接话也判 yes。
  ⚠️ 风险：播客/电视也可能像两人对话 → 误判 yes 时环境声会误开；本轮只测到会议正样本。
- 验证：host **98 组 / 7967 断言**（新增 2 组）；离线重放真实分段逐段调千问：
  17:31–17:33 **14/14 yes**（原 0）、17:29–17:31 9/11 yes；17:08–17:11（像播客/独白）0 yes、
  16:58 耳语独白 0 yes。
- 执行端：17:48 起 host 为 `--ambient-executor relay`（快照 `executor=s3-relay`、
  `available=true`、device=Xiaomi 耳夹，`opens=0`），门仍 `--ambient-gate voice`。
  **待真机验收（下一步）**：真实对话时面板判定 yes、环境声卡片"中继已开"、耳机听到
  中继声（Xiaomi 总时延约 385–405 ms）；独白/电视应保持不开。若不 open，优先看
  `authorization` 的 epoch/age（`assistant.ambient_authorization`）与门时序。
- 详见 `docs/decisions.md` D55；本轮改动：`tools/host/assistant.py`、
  `tools/host/tests/test_host.py`。

## 最新补充（2026-10-28 傍晚 · ASR 路由修复 + 真机耳语验收，提交 `8695186`）
- 问题：短/稀疏耳语送千问返回 HTTP 400（空体=无人声），面板写成"未检出人声/未转写"，
  用户说了话却拿不到正文；旧云端路径还会把候选按能量 VAD 裁剪、按声纹边界拆成
  0.3–1.7 s 多次请求。
- 修复：① worker `speech_seconds < 3 s` 先走本机 ASR；② 千问 400 时本机兜底，
  本机不可用才回 `transcript_nospeech`；③ host 千问路径保留整段候选、单次请求，
  混合身份 fail-closed 降 unknown。不改声纹阈值、不自动学习耳语。
- 回归（实测，非文档旧数字）：host **96 组 / 7958 条断言**、`test_qianwen_api` **14/14**，
  均 exit 0。⚠️ 此前报的 94/7950 与修复前相同：2 个新测试**没注册进 runner**，且其中一个
  PCM 断言漏了 `_prepare_asr_audio` 的 +18 dB 归一化——已修。
- 真机验收（修复后代码，session `20261028-165345`）：耳语 2.32 s（RMS −36.1 dBFS）→
  **本机 ASR**"测试，一二三四。"（notes 含「短/稀疏语音由本机 ASR 优先识别」）；
  耳语 3.44 s → 千问直接出正文（无 400/无兜底）；正常音量 5.14 s（−20.9 dBFS，
  asr_gain 0.9 dB）→ 千问"12345678"（latency 1165 ms）；`asr_error` 空。
- 声纹：耳语窗口 self 0.87–0.96 但含混合片 → 归属 unknown、LLM unclear；
  `learned-prototype.npz` 停在耳语前 16:58:09、`learn_updates=898` 不变 → 未误学。
- 已知边界：更小声（−41 dBFS，seg16/17）仍被能量 VAD 判 0 s 语音跳过——是 VAD 门限，
  不是 ASR 路由；本次未改，避免扩大误触。
- 现场已收尾：`rec stop` ack（capture stopped / recording false）→ `/api/stop` 释放串口，
  8787 已停，麦克风不再采集。工作树干净（无关的 systemeq 改动未纳入本次提交）。

## 最新补充（2026-10-28 上午）
- 工作树里 2026-10-26 晚/10-27 的两处 host 修复已提交 **`1bade26`**（环境声门只认
  LLM 语义确认；滚动分段遥测/声纹对齐）；**A1/D50 已实现**（见下节，代码待提交）。
- 文档同步：D42 加"2026-10-26 晚更新（触发源收窄）"；新增 D50（LLM 迟到否决回写 S3）；
  `product-alignment`/README/TODOS/plan-status 同步。
- **设备当前未连**（无 `/dev/cu.usbmodem*`，8787 未运行）：D49 贴耳打字听感验收、A1 刷机
  验收都要等设备到。固件二进制已按 A1 编译好（`build/cores3_noise_adapt_demo.bin`）。
- 下方"最新补充：千问接口切换"为 10-26 记录，HEAD 行已过期，以本段为准。

## 本轮内容（2026-10-28 · D50 / TODOS A1：LLM 迟到否决回写 S3 快路径）
- 控制行：S3 新增 `dialog no [ttl_ms]` / `dialog yes`（纯文本，ack `dialog ack ...`，
  非 JSON；TTL 缺省 20 s、上限 120 s）。host 只在否决**新鲜**（`age_s <= ttl_s`）时回写，
  每 5 s 续期；撤销/判定过期/退出时发一次 `dialog yes`。默认 `--ambient-executor off`
  也回写——默认 demo 最需要它（环境声本就关，但 S3 对话态会压平 EQ、停掉掩蔽）。
- S3 行为：TTL 内只否决"是不是对话"——engaged/anticipating 立即转 releasing（媒体平滑
  回 100%、ambient_open 关），TTL 内新证据不认；背景人声回落 `EXTERNAL_VOICE_INTENT`
  掩蔽。TTL 到点端侧自 fail-open 回 D16（host 崩了不会永久压平 EQ）。
- 遥测：命令帧追加 `dialog_veto`（不涨 schema）；host protocol/state 解析为可选键。
- 验证：C++ 12/12（controller 127/127、conversation 67/67）；host 94 组 / 7946 断言；
  千问 11 项；**固件编译通过（未刷机）**。待真机：连续电视/播客下 LLM no 后设备报
  `dialog_veto=true`、mode 从 FLAT 回 SONG_EQ/EXTERNAL_VOICE_INTENT。
- 现场刷机验收命令（设备到场后）：
  `sh tools/free_port.sh --kill` → `sh tools/build.sh flash` → `sh tools/host_up.sh --detach`
  → 面板看 mode/`dialog_veto`；串口 monitor 可见 `dialog ack no 20000`。

## 本轮内容（2026-10-28 · D51：混噪/敲击下掩蔽不再闪断）
- 2026-10-28 现场第一次"贴耳 + 音乐 + 打字"验收（`记录/位置对照/20261028-d49-ear-typing.jsonl`，
  124 s / 1203 帧，采集脚本 `/tmp/d49_accept_recorder.py`）：D49 目标达成——FLAT 4%
  （D49 前 47%）、conversation 从不 LIKELY、media 全程 100。
- 但抓到 34 次 EQ 闪平（100–400 ms）：单帧瞬态（敲击/音乐鼓点/漏音）被 1 s 事件窗标成
  speech、voice_likelihood 顶到 1.0 → 状态机首帧进 anticipating → 旧写法立刻压平 EQ。
  修：`conversation_active` 改用 `awareness_out.anticipating`（≥500 ms 预览确认）才压平。
  代价：真实对话 EQ 压平晚 ≤400 ms（媒体预下压时序不变）。controller 128/128。
- 混噪识别现状（供下一步）：设备录的键盘瞬态在 2–4 kHz 很干净（打字时冲击率 5–30 次/s、
  高出底噪 16–36 dB；不敲 <1 次/s），但 event 分类器在 1 s 窗里基本看不到（打字段
  keyboard 帧只占少数，多数标 speech/quiet；音乐内容大量标 speech）。下一步：帧内瞬态
  检测器驱动 D48 键盘掩蔽 + 把瞬态帧按非语音处理。
- **2026-10-28 12:26 已刷机 D50+D51**（`sh tools/build.sh flash`，Hash verified；重启后
  帧里已有 `dialog_veto` 键、`mic2_valid=true`）。同时验证到 D50 的一个真机问题并修正：
  **本地否决（local_veto）也回写**会让静音时的 ASR 段把 S3 快路径锁在"释放"TTL 里，
  现在只回写 `llm_veto`（见 D50 2026-10-28 修正段）。host 94 组 / 7947 断言。
- **2026-10-28 12:3x D52（systemeq，host 侧）**：实测 EQ 输出峰值 1.8、0.89% 采样硬削波
  → 用户听到的"人声毛刺"。加 EQ 后输出计量（`out_peak`/`out_clip_frames`）、50 ms 拍
  自适应 headroom（只收正动态增益 0…2 dB）、正增益总预算 5 dB；**不要加宽滤波带宽**
  （0.55 oct 让并联段重叠、等效增益翻倍：峰值 1.8→4.0、削波 4.6%）。
- **刷机后 150 s 验收数据**（`记录/位置对照/20261028-d51-d52-accept.jsonl`，1441 帧）：
  * D51 验证：FLAT 只剩 **7 帧 / 5 段**（旧 34 段/124 s），且全部 ≤200 ms、都是
    "confirmed anticipation（500 ms 证据）"或 artifact（削波帧）——单帧瞬态闪平消失；
    conversation 从不 LIKELY；media 全程 100。
  * D50 修正验证：`dialog_veto` 全程 false；静音时不再下发 `dialog no`。
  * D52 计量：150 s 累计削波 0.2%（改前 0.89%）；headroom 在 0–2 dB 间自动调节，
    applied 峰值 2.0–5.0 dB。剩余削波集中在"响歌 + SONG_EQ/quiet"与
    "音乐漏音被标成 speech → EXTERNAL_VOICE_INTENT"（12% 帧）——见下一步。
- 当前在线：D50+D51 固件 + 新 systemeq/host；host 日志 `/tmp/cores3host/host.log`。
- **用户听感反馈（2026-10-28 14:33）**：D51+D52 后"好多了，真的好多了"；但"贴键盘掩蔽
  超级准，放肩膀上（近耳）就不准"，具体是**"敲击时掩蔽太弱 + 节奏对不上"**。
- **双位置数据（2026-10-28 14:33–14:39，`记录/位置对照/20261028-position-ab.jsonl`）**：
  贴键盘 keyboard 命中 52%、掩蔽抬 2.6–3.4 dB；肩上命中 15–21%、掩蔽抬 1.2–2.2 dB；
  肩上情境模型 25–33% 帧判 music（自放漏音），偶发 speech→EXTERNAL_VOICE_INTENT。
- **D53 已实现并刷机（2026-10-28 14:50）**：帧内 8 子块 HF-crest 检测（≥9 dB = click），
  1 s 内 ≥2 次 → `typing_active`（保持 300 ms），并入 keyboard_event / non_voice_event；
  遥测 `typing_active`。离线：noise_analyzer 51/51、controller 128/128、host 94/7950。
  **待现场验证**：肩上打字时 typing_active 占比、键盘掩蔽抬升、听感（稳/够不够）。
- **D53 第一版现场追加（2026-10-28 15:05，已再刷机）**：① 轻敲不明显 → 帧内瞬态阈值
  9→**7.5 dB**；② "完全安静后歌曲调 EQ 那一下唐突" → 键盘掩蔽改独立包络（起音 250 ms、
  释放 **2600 ms**，原与噪声掩蔽共用 400/1200）；噪声掩蔽不变。noise_analyzer 51/51、
  controller 128/128。待现场听感（轻/重敲 + 停手后的 EQ 回落）。
- **D53 第一版数据（14:53–14:58 录制，`记录/位置对照/20261028-d53-verify2.jsonl`）**：
  重打段 `typing_active` 35–60%、gainKb 2.9–3.05；轻打段 typing_active 0–20%、gainKb 1.5–2.0
  ——这就是"轻敲不明显"的量化依据；停手 2 s 内 gainKb 从 3.0 掉到 1.7（回落太陡）。
- **D54 已实现并刷机（2026-10-28 15:2x）**：现场"歌曲声音忽大忽小"。数据
  （`记录/位置对照/20261028-d53-verify3.jsonl`）：① 音乐漏音让 1 s 窗偶尔报 speech，模式
  在 SONG_EQ↔EVI 跳，presence 抬升 1.76–2.73 dB；② 转头/摩擦触发 wind（15 s 里 24 帧），
  wind EQ 把低频抬升 +0.3→−0.2 dB；③ systemeq headroom 在 1.3–2.0 之间抽（回落 0.4 dB/s）。
  修：EVI 滞回（进 300/出 500 ms）、wind 去抖（连续 ≥800 ms 才生效、停后保持 2 s）、
  headroom 回落 0.02→0.004/50ms。controller 133/133、ctest 12/12；已刷机+重启。
- **D54 数据（2026-10-28 15:03–15:08，`记录/位置对照/20261028-d54-verify.jsonl`，300 s）**：
  * 模式切换 **36 次**（D54 前同条件 179 次/300 s）；headroom 中位 **1.98 dB**、基本不抽；
  * 正常打字段：typing_active 很高、键盘掩蔽抬 **2.4–3.1 dB**；重打段（bb −36 左右）抬
    **3.3–3.5 dB**——轻/重敲的剂量关系是对的；
  * `typing_active` 总占比 34.4%；wind 115 帧（去抖后低频不再跳）。
  * 异常待查：t≈280 s（15:08:12）出现 75/96 帧 FLAT + gain≈0.1（当时未在打字、bb −36）；
    原因未定（可能是安静分支/遮麦/状态机），下轮看一下。
- **键盘轴体口径（用户 2026-10-28 补充，重要）**：现场标定用的是**线性轴（非段落）机械键盘**
  ——触底声较闷、帧内高频瞬态少，属于"更难检测"的一侧；段落轴/青轴高频更尖，同阈值下
  更容易触发也可能误触发。跨键盘稳定前，demo 口径应写明"标定基线 = 线性轴机械键盘"。
- **下一步**：① 用户听感结论（转头忽大忽小/停手回落）；② 查 t≈280 的 FLAT 异常；
  ③ "音乐漏音不当作背景人声"——建议加 host→S3 `music on|off` 控制行（S3 不知道音乐是
  自己放的）；④ A0 真人对话验收仍未做。
 
## 最新补充：千问接口切换（2026-10-26）
- 分支 `feature/apple-conversation-awareness`；千问轮改动已于 10-26 提交，HEAD 见 `git log`。
- LLM 改用千问 OpenAI Chat API：`qwen3.7-flash`、关闭 thinking、JSON；ASR 新 worker 使用 `qwen-audio-3.0-asr-flash`，WAV Base64 上传。provider 可分别回退；配置见 `docs/qianwen-api.md`。Key 仅在 gitignore 的 `.env`，0600。
- 云端转写失败（含部分轮次失败）保留错误并发布 unclear/close，不再当作空输出判 no；不改声纹算法/提示词/执行门/固件。
- 验证：新增 10 项测试、原 host 90 组/7917 断言通过；官方 3.83s 音频上传通过；生产提示词的人工构造问答/播音/身份不足分别 yes/no/unclear。
- 8787 当前 serial connected；LLM 千问 ready，ASR worker 就绪，EQ basic 和声纹可用；ambient-executor 仍 off。旧代理 403 不再是当前 LLM 状态。
- 待验：现场 ASR HTTP 400（空错误体）；有效示例语音通过、人工静音可复现，真实房间原因未定。下一步在面板说一句完整的话，检查转写/ASR 错误。不能宣称真人对话或听感验证成功。
- 启动用独立会话包裹 `sh tools/host_up.sh --detach --ambient-executor off`，防工具退出回收后台进程。不要重复占串口。

## 仓库状态
- 分支 `feature/apple-conversation-awareness`；工作树干净（host 修复已提交 `1bade26`；
  `.gitignore`/docs 本轮另提交）；远端未 push（origin 仍停在 66cdbd9）。
- 测试口径（2026-10-28 实测）：host 93 组 / 7932 条（exit 0）、千问 11 项全过；
  ctest 12/12、controller 117/117。

## 本轮内容（2026-10-26 下午 · D48 键盘/普通噪声特殊掩蔽）
- 用户反馈"EQ 基础状态遮蔽一般，尤其键盘噪声"→"没让你物理隔音，要你做特殊掩蔽"。
  定位（8787 快照）：键盘来时 event=keyboard、-42 dBFS、SONG_EQ、掩蔽推量≈0、内容曲线
  ±0.8 dB——D41 掩蔽只认人声，键盘没有任何掩蔽路径。
- 实现：`AdaptController::update()` 加 `keyboard_event` 参数；SONG_EQ 内两条掩蔽：
  ①普通噪声 宽带 +0.3、presence(1.6–4k) +1.0，深度 -54→-34；②键盘 宽带 +0.5、
  足迹段(1.0–3.4k，D17 质心 932 Hz) +1.5，深度 -52→-34。**深度独立慢平滑 400ms/1.2s**
  （每拍更新，flat/safety_hold 也衰减）。对话/自身语音=flat、安全保持只削不抬，豁免（D25）。
- **现场迭代**：第一版"拉爆"（足迹段 +4、696.5 Hz 起）→ 用户："人声完全变了、毛刺感极强"；
  第二版幅度砍约 1/3 + 足迹下沿上移 1 kHz + 深度慢平滑，已刷机（两次）。
- 8787 host 已按默认三件套重启（QQ音乐 systemeq basic / ambient-gate voice / voiceprint）。

## 本轮内容（2026-10-26 17:0x–17:3x · D49 对话证据过事件层 speech）
- 用户发现"S3 贴键盘掩蔽正常、贴耳朵不准确"。双位置采集（`记录/位置对照/20261026-kbd-ab.csv`）：
  电平只差 3 dB，主因是**耳边 OWS 漏音** → quiet 窗口 vl 0.82 → 状态机锁 engaged →
  FLAT 占 47%（键盘边 16%）、最长 1.9 s → 掩蔽被压平。
- 修法：对话证据(0.65)与背景人声判定(0.55)必须过**事件层 speech**（`update()` 加
  `speech_event`；app_main 从 sound_event_out 算）。背书：真人面试回放证据帧 1464/1464
  本就是 speech、连续段统计零变化；漏音打字 speech 0 帧。代价：对话首响 +~1 s。
- 离线工具 conversation_replay / voice_decision_offline 串入同一事件检测器（口径一致）。
- 感知点口径（写进 product-alignment）：S3=耳机仓=人身边；贴耳不是产品几何但 D49
  让掩蔽在贴耳下也能连续工作。
- 已刷机（D49），8787 host 已重启；**待用户贴耳打字听感验收**。

## 本轮内容（2026-10-27 10:0x · 滚动分段遥测/声纹对齐修复）
- 根因：`observe_frame` 消费 preroll 后**没清空**，它的第一项永远是最早那块音频 → 滚动
  分段（6 s 上限）后 `_session_start_sample` 停在旧值。后果：① 遥测 `sample` 与音频基准
  错位 → 键盘/风/异响排除静默失效（现场"含事件帧 24 段只有 8 段真排除"）；② 声纹窗按
  绝对流样本号匹配 → 第 2 段起对不上，speaker 全落 unknown（"分不清 self/unknown" 的一半）。
- 改（`assistant.py`）：preroll 消费后 `clear()`；连续流下缓冲为空时起点跟随当前游标；
  60 s 上限裁剪开头时起点同步前移。
- 设备事件证据规则放宽：`event=speech` 连续帧也算语音证据（vl 低也算）。但
  `_TRUST_DEVICE_EVENT=False`（2026-10-26 晚口径：固件 event 会把语音错判 keyboard/wind），
  默认仍只用能量 VAD；固件 event 可靠后把开关翻 True 即生效。
- 回归测试：滚动两段后 `start_sample` 必须等于该段首块音频、起点+缓冲=游标、段内键盘帧
  能生成排除区间；设备 speech 事件帧能构成证据。host 93 组 / 7932 断言 + 千问 11 项全过。
- 8787 已重启（10:0x，serial 真机，relay 未开）。声纹在线学习累计 updates=777，
  原型与注册均值 cos=0.95（未污染，必要时删 learned-prototype.npz 回退）。

## 未完成 / 下一步
- **2026-10-28：设备未连、8787 未运行**（无 `/dev/cu.usbmodem*`）。设备到场后第一件事：
  D49 贴耳打字听感验收；8787 用默认三件套 `sh tools/host_up.sh --detach` 拉起
  （先 `sh tools/free_port.sh --kill`）。
- **⚠️ 待用户贴耳打字听感验收 D49**；掩蔽幅度仍不满意改 controller.cpp 的
  `kNoiseMask*` / `kKeyboardMask*` 常量，改后 ctest → free_port → flash → host_up。
- 键盘掩蔽在 SAFETY_HOLD 豁免：猛敲把 250 ms 平滑电平顶过 -30 dBFS 仍会有模式边界抽动
  （D38 既有）；若仍刺耳可给 keyboard_event 加模式滞回。
- 下一步代码项：~~A1~~ **已实现（D50，待刷机+真机验收）**。真机验收场景：连续电视/
  播客说话时看设备 `dialog_veto=true`、`mode` 从 FLAT 回 SONG_EQ/EXTERNAL_VOICE_INTENT，
  且 LLM 判 yes 后能恢复对话下压。
- 遗留（不变）：A0 真人验收、#38 房间本底、D47 三态阈值、6.5 vs 8.0 dB、
  声纹 self-veto 默认关、other 快锁。

## 当前环境（2026-10-28 12:1x）
- 8787 host **未运行**；CoreS3 **未连**（无 `/dev/cu.usbmodem*`，只有蓝牙端口）；
  工作树已按"固件 / 上位机 / 文档"提交 D50（HEAD 见 `git log`）。固件二进制已按 D50
  编译（`build/cores3_noise_adapt_demo.bin`，未刷机）。
- 设备到场后：`sh tools/free_port.sh --kill` → `sh tools/build.sh flash`（刷 D49+D50）→
  `sh tools/host_up.sh --detach`（默认三件套：QQ音乐 systemeq basic + ambient-gate voice +
  voiceprint）。先验收 D49 贴耳打字掩蔽，再做 D50 连续播客/电视验收。LLM 千问（见上方）。

## 千问 ASR 400 根因（2026-10-26 16:4x 只读诊断；结论已落到 10-26 提交的 400 语义）
- 结论：模型没坏。实测干净 TTS / -52 dBFS 弱语音 / SNR 0 dB 混噪 / 400 ms 短句均 200；HTTP 400 `{}` 只出现在窗口内无可识别人声（静音、键盘、房间噪声）。seg0001 最响 1.5 s（键盘）400、最安静 2.5 s 200（"你是写出来了。"）；本地 qwen3-asr 判断一致（噪声→"嗯。"）。
- 现场 15:36 会话 457 段：400=222、跳过=178、真实转写≈1 条（seg0129）；当前快照 segments=518/passed=30/skipped=173/filtered=315，asr_error 仍显示 HTTP 400。
- 根因在上位机门控：能量 VAD 把键盘瞬态当语音（设备 voice_likelihood 贡献 0 s）；`_MIN_SPEECH_S=1.0`（assistant.py:86）把 0.4–1.0 s 短句整段跳掉（seg0376 证据 0.84 s → 跳过）；任一切片 400 → 整轮 unclear 且跳过 LLM（:327），面板写"请求失败"（:284）；`qianwen_api.py:41` 丢弃错误体 → 现场只见空 400。
- 建议（多数已实施：400→无人声、`_MIN_SPEECH_S`→0.55；谱平坦度门未做）：400 软降级为"无人声"；发送前加周期性/谱平坦度门（噪声<0.15、语音>0.25）；`_MIN_SPEECH_S` 降至 0.45–0.6 s；记录脱敏错误体。
- 证据：`记录/训练数据/20261026-153627/`（seg0001-3=400、seg0129=200、seg0376=短句被跳）。

## 已实施（2026-10-26 17:0x，已于 10-26 提交 `322d455`/`478e3fb`）
- ASR：`qianwen_api` 加 `ProviderHTTPError/AsrNoSpeech`（400=无人声）；worker 回 `transcript_nospeech`；host 按"这段没听出内容"处理（不再"请求失败"）；部分片段失败但拿到正文→"部分片段转写失败"。设备事件与 ASR 耦合：`_BLOCKING_EVENTS`（keyboard/wind/artifact）时段从语音证据里挖掉（`_event_blocked_regions/_subtract_intervals`），全被覆盖则跳过不调用。`_MIN_SPEECH_S 1.0→0.55`。
- 声纹：`--unknown-margin 0.15→0.30`（中间区分 unknown 不误判 other）；`--window-s 1.5→3.0`（现场实测 1.5 s 窗本人 0.65–0.75、噪声 0.65 不可分；3 s 窗本人 0.86–0.90、键盘/噪声 0.65–0.71）。
- 测试：host 92 组 / 7925 断言、qianwen 11 项全过；8787 已重启（17:07），现场已见"服务端未检出人声"不再写请求失败。
- **现场验收（17:20，重启后新会话 20261026-171849）**：self 触发 15 窗（0.89–0.98）；seg15 转出 `self：测试一下声纹，1234567`、seg14 `unknown：测试一下。 / self：要升温。` → LLM 判 yes/open（0.85–0.9）；噪声段显示“未检测到有效人声/服务端未检出人声”，不再出现“转写请求失败”；asr_error 空。
- **声纹已现场重注册（17:18）**：注册句 3 遍落在 `记录/训练数据/20261026-170705` seg0089/90/91，用 `tools/enroll_from_segments.py` 生成 `记录/声纹/self-device/live-*.wav`；旧目录（含 learned-prototype.npz）整体备份为 `记录/声纹/self-device-20261019`。离线校验：本人 0.95–0.98、同事 0.25–0.43、键盘噪声 0.50。host 已重启：voiceprint clips=3、updates=0、learned_from 空。待验：真人说话时快照 history 出现 self（≥0.8）且 ASR 正常出转写。

## 验证入口
```bash
cmake -S test -B build-host-tests && cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
python3 tools/host/tests/test_host.py
# 重刷（先停 host）：
CORES3_PORT=/dev/cu.usbmodem101 sh tools/free_port.sh --kill
sh tools/build.sh flash
sh tools/host_up.sh --detach
```
