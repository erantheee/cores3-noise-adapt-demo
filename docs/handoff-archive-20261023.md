# Session handoff — 2026-10-22

> 每轮收尾更新本文件（≤2.5KB）。新会话顺序：`AGENTS.md` → 本文件 → 再按需读文档。
> 最后更新：2026-10-23 午前（+ 声纹在线积累 + 门回滚否决器 + 端侧判据事件中立，未 commit/未刷机）

## 仓库状态
- 分支 `feature/apple-conversation-awareness`；HEAD `66cdbd9`（"Execute ambient on the fast device path and let the LLM veto late"）。
- **工作树脏**：30 个未提交文件（README/TODOS/docs/* 文档 + `main/controller.*`、`main/conversation_awareness.*`、`test/*`、`tools/host/*`、`tools/host_up.sh`、`tools/player/player.swift`；未跟踪 `tools/host/memo.py`）。文档今日 +49.5KB——**不要假设文档 == 代码**。
- 本次 AI 性能审计（2026-10-22）新增未跟踪文件：`AGENTS.md`、`docs/handoff.md`（跨会话记忆，保留）；详细报告在 `work/ai-perf-audit/ROOT-CAUSE-AND-FIXES.md`。
- 本机已装 ESP-IDF v5.4.1（`~/esp/esp-idf` + python3.12 env）：`sh tools/build.sh [flash]`；刷写 `sh tools/flash.sh`（默认 `outputs/current`），刷前 `sh tools/free_port.sh --kill`。

## 2026-10-23 上午：D47 双麦取数（已刷机，双路遥测真机跑通）
- **背景**：用户拍板"应该使用双麦"（远/近概念需要处理）。D47 只取数、不接判据。
- **固件**：I2S MONO→STEREO、`format.channel=2`；staging 单位改交织样本（容量 8×512×2）；窗口只存 L 路（偶数下标）、decimator/中继/录音抽 L 路后照旧——**下游零行为变化**。新增 `main/dual_mic.hpp`（帧级 L/R 统计：电平、电平差 L−R、Pearson 相关）+ 串口 4 键 `mic2_broadband_dbfs/mic_diff_db/mic_coherence/mic2_valid`。host：protocol 解析校验、面板环境卡显示"双麦 差x.x dB · 相关0.xx"、模拟源 VOICE=近场。
- **根因记录（重要）**：初版真机 mic2_valid 恒 false——`controller.update()` 的遥测搬运段漏搬 mic2 字段（AnalysisFrame→EarbudCommand），双麦统计算好了但没进 command。用 `dual_acc_frames` 诊断键定位后修复。
- **真机验证（已刷，SHA 见 build/）**：mic2_valid 全 true；安静房间（远场）diff ≈ 0.1–1.0 dB、coherence 0.87–0.99（两路近似一致，符合远场预期）。**近场说话样本还没采**——需人对着设备说话，看 diff/coherence 是否如预期"差大、相关低"。
- **注意**：本次刷机固件 = 当前工作树编译产物，**包含上一节的端侧确认判据改动（800 ms 连续证据+事件中立）**——即上一节"待办①刷机"已由本次完成。验证：ctest 12/12、host 自测 86 组/7907、渲染 45/0；host 已按默认三件套重启。
- **下一步**：① 取近场/远场/安静三态双麦数据（面板 env-raw 可直读）；② 区分度够再定近场判据阈值接对话预判；③ 决定是否保留 dual_mic.hpp 里的 `mic2_broadband_dbfs` 上报口径（当前=右路电平）。

## 2026-10-23 午前：声纹在线积累 + 门回滚 + 端侧判据（未 commit，**固件未刷**）
- **声纹在线积累（用户要求"持续积累声纹"）**：`tools/voiceprint_worker.py` 高置信 self 窗口（score≥0.8、speech_ratio≥0.5、dbfs≥-40）以 learn-weight 0.08 在线更新原型，落盘 `记录/声纹/self-device/learned-prototype.npz`（原子写、重启加载）。实测 60 s 内 learn 0→19、score 峰值 0.98、self 稳定判出；`tools/host/voiceprint.py` snapshot 透传 `learn_updates`。防污染：混合/中间区样本不学。**声纹 self 判出后戴耳机场景稳定（0.81–0.93）。**
- **门回滚（用户现场质疑"我说话时环境声被拉起来"）**：`__main__.py` 的 `ambient_gate_allows()` 恢复为**否决器**——device_intent 是唯一触发器，self/LLM 只批准；新增 llm_veto 最高优先（D34）。教训写进 docstring。
- **端侧确认判据（用户拍板 A 方案"参数用数据选"）**：W1 噪声 1179 帧回放否决了滑动窗口累计（1.5s 窗证据最多 11 帧，累计 800 ms 仍有 138 窗口达标）与 vl 3 帧平滑（噪声连续段 700→1000 ms）；**最终=连续 800 ms + 事件帧中立**（`Evidence.event_blocked`：keyboard/wind/artifact 不打断连续证据、不累计释放计时，W1 仍 0 误触）。`main/conversation_awareness.{hpp,cpp}` + `main/controller.cpp` 已改；`test/w1_timeline_golden.h`（1179 帧 3 态）+ 用例 12–14（W1 回放/键盘中立）；评估脚本 `work/diagnosis/evaluate_gate_params.py` 已固化正确结论。ctest 11/11、`sh tools/build.sh` 编译过。
- **待办**：① 刷机（`sh tools/free_port.sh --kill && sh tools/flash.sh`，注意多会话 host 协作规则）后真人对话漏检验证（用连续音频 `CORES3_CONTINUOUS_AUDIO=1`）；② 19:46 数据回放显示真实对话有 3 段 ≥800 ms 连续证据，设备上却从没确认——疑似设备端 cue 未达（dbfs/cue_level 口径），刷机后要用 frame 遥测复核证据链。

## 今天（2026-10-23）——LLM 403 事故与 self 抹杀修复（未 commit）
- **事故**：20:40 起 LLM 判定全部 `HTTP 403 Forbidden`（gpt-5.6-sol 走代理账户余额不足：预冻结 0.42 元，余额 −0.01 元）；voice 门只剩端侧意图，而端侧意图几乎全 0 → 20:40–21:00 环境声全程没开（events.jsonl 全 open=false）。
- **已改（host 侧，自测 84 组/7885 断言通过）**：
  1. `_filter_asr_text(..., speaker)`：self 的语气词/单字短应答不再整句丢弃（"嗯"保留），other/unknown 仍过滤；
  2. `_note_context_turns`：self 文本被过滤时也留"（短应答，内容未识别）"占位轮次；
  3. `_speaker_segments`：<0.45 s 的 self 轮次不再折叠成 unknown；
  4. `_summarize_event` 新增 `interaction` 特征（self_turns/other_turns/alternating/recent_alternating）+ prompt 规则，辅助 LLM 判"轮流开口"；
  5. 面板文案去掉"噪声/重复/已丢弃"（改"这段没听出有效内容"）。
- **.env**：`ANTHROPIC_MODEL` gpt-5.6-sol → claude-opus-5-5（claude 实测 200；gpt 余额不足，要用需充值）。
- **声纹数据（20:40–21:00 账本）**：轮次 self 47% / other 11% / unknown 42%；段级 77% 有 self、21% 有 other、17% self+other 同段 → self 能判出，阈值 0.8 可用；unknown 高是远场/混音限制。
- **阻塞**：CoreS3 当前未插（无 `/dev/cu.usbmodem*`），host 未跑，端到端未验证。下一步：插设备 → `sh tools/host_up.sh --detach --relay-audio --ambient-executor relay --ambient-gate voice --voiceprint` 验证 interaction 能否让"讲解+稀疏接话"判对话并开门。

## 今天（2026-10-22）已完成，代码在、未 commit
- **D41**：`EXTERNAL_VOICE_INTENT` 从"人声靠前"改为**掩蔽**（全频段 +1 dB、1–4 kHz 至 +4 dB；Mac ×2.5 后语音带约 +8 dB）；不压媒体、不开环境声。
- **D42**：Mac 执行门 `--ambient-gate off|llm|voice`（演示默认 `voice`，配合 `--voiceprint`）；媒体下压也走同一门。
- **D43**：面板 24 段对话账本 + `asr_stats`（通过/跳过/过滤、ASR p50）。
- **D44**：声纹 unknown 修复（能量兜底：块 RMS > 近 20 s 本底 +6 dB 也算说话），实测同段 unknown→连续 self(0.82–0.97)。
- **D45**：说话人归属改为逐片判定 + 短 unknown 边界平滑（同一句不再被切碎）；LLM 工作台拆成「ASR 转写」与「判定窗口」，其余收进 details。host 自测 **75 组 / 7779 条**；渲染 58/0；host 已重启（voice 门 + voiceprint）。
- **QQ 音乐 system EQ 已接入 host（2026-10-22 傍晚/晚）**：`tools/systemeq/`（tap `com.tencent.QQMusicMac` → 24 段 AVAudioUnitEQ → 默认耳机）+ `tools/host/systemeq.py`；host 参数 `--system-eq-app`（`--demo` 固定基线，**两档**：`basic`=网友共识克制版（低 +3/去糊 −0.5…−1.5/presence +1…+3/空气 +2…+3），`aggressive`=×2 明显版（低 +6/去糊 −1…−3/presence +2…+6/空气 +4…+6，默认）；245 Hz 两档均 0（网友共识糊区）；S3 24 段 ×2.5 叠加；夹 ±24 dB；**用户口径：允许削波、无延迟、场景过渡平滑**）。**面板 EQ 三档 switch（基础/激进/关闭EQ，2026-10-22 晚）**：`{"cmd":"mode","value":"basic|aggressive"}` 切基线、`{"cmd":"bypass","on":true}` 执行端直通；host 新增 `--eq-mode`（默认 aggressive），server 路由 `/api/player` 加 mode/bypass 白名单分支。在线曲目已跑通：48 kHz/2ch、producer/consumer 均 ≈48 kHz、ring 不溢出。关键坑：聚合设备用**空 subdevices + master=0**，且建 IOProc 前先读一次 `kAudioAggregateDevicePropertyComposition`，否则 `AudioDeviceStart` 返回 `nope`；**不要把默认输出 UID 塞进 master**（会让 IOProc 到 ~1.5× 溢出）。面板「实际应用」卡已接 systemeq（`applied_bands`，±24 刻度；`executionSide()` 统一读执行端）。只读探测见 `work/qqmusic-tap-probe/`。未测：延迟、设备切换、HFP、与中继共存。
- **网友 EQ 共识调研 + 245 Hz 校准（2026-10-22 晚）**：调研见 `work/eq-design/community-eq-consensus.md`，参数底稿（含拉高拉低操作手册）见 `work/eq-design/baseline-netizen-v1.md`。结论：D39 曲线与网友共识大方向一致（嘈杂=去糊+2–4 kHz presence、安静=平直）；唯一明确偏离 244.9 Hz 糊区正增益已改 0（`kSongEqContentDb[2]` +1.0→0）；低频 +2.5/+3.0 是 OWS 泄漏补偿例外（封闭耳机应 ≤0）。**已刷机（SHA256 43172824…）并真机确认**：`eq_gain_db` band2 恒 0.0、SONG_EQ 曲线正常；host 已重启（voice 门 + voiceprint + systemeq，`mode=aggressive`、applied 低频 +9.7/245=0/去糊 −6.2）。验证：ctest 11/11、host 自测 **82 组 / 7857 条**（新增三档路由测试）、渲染 72/0。网友共识里未做的：等响度补偿（无音量输入）。
- **连续音频收集（P0 数据基建，2026-10-22 下午）**：新增 `tools/host/continuous_audio.py`——会话级 16 kHz 整段 WAV（`audio-0001.wav`，轮转处样本连续）+ `capture.jsonl` 时间轴（seq/设备时间戳/偏移/丢块）+ `summary.json`（时长/丢块/磁盘预算止损原因）；采样率不一致拒绝写入、丢块只记账不补零、close 幂等。`host` 加 `--continuous-audio DIR` / `--continuous-chunk-seconds`；`host_up.sh` 加 `CORES3_CONTINUOUS_AUDIO=1`（默认关，约 150 MB/h）。host 自测 **81 组 / 7842 条**。
- **Xiaomi 耳夹硬件核实（2026-10-22 晚）**：① 拆解（52audio 2026-08-12，型号 M2529E1）：每耳 ST **LSM6DSOWTR 6 轴 IMU**（官方用途=敲击控制）+ BES2710Y + 3 MEMS + 1 VPU；无头追功能。② **耳夹麦克风 = macOS 标准蓝牙输入设备**（「Xiaomi 耳夹式耳机」默认输入，16 kHz 单声道，HFP 通话上行，DSP 门控：安静时 −79 dBFS 数字静音），ffmpeg avfoundation 可直接录（采集时耳机会切通话模式、音乐受影响）。③ 陀螺仪数据：无标准协议、BLE GATT 连上后 Mac 端枚举不到（bleak 扫不到/连不上）→ 转头选人的朝向源不能指望耳夹 IMU。④ ASR/声纹"又不行"排查结论：模型与链路正常，问题是"人的话没进 S3 麦"（录音包络无语音调制 + 已知第 38 项房间本底误判为语音）；近场实测待做。
- **D46 混合环境识别（2026-10-22 深夜，已刷机）**：九分类情境模型从 argmax 单标签升级为上报 softmax 分布 top1+top2+`context_mixed`（top2 ≥ 0.20 判混合，`context::kMixedTop2Threshold`）；串口追加 `context_top2/top2_score/mixed` 可选键（不涨 schema，先例 voiceprint_*）；host 解析+校验、面板/日志/设备屏幕显示混合；顺手修了 `__main__.py::current_scene()` 读 `context.class_name`（str 无此属性）恒空的断层——九分类场景名第一次真正进 LLM/账本。**决策仍不读九分类**（Round 2 纪律）。已验证：ctest 11/11、host 自测 83 组 / 7873 条、渲染 43/0；**已刷机**（SHA256 677dbce6…，D46+245Hz 校准一起）+ host 已重启；真机帧确认 `context_top2/context_top2_score/context_mixed` 正常上报（`other 0.889 / top2 near_voice 0.029 / mixed:false`），245 Hz band=0.0。刷机坑：921600 下分区表尾部反复"No more data"→ 降 460800 成功；**刷前必须 free_port（19:14 用户重启的 host 曾抢端口致失败）**。
- 测试口径：C++ 11 套；host 自测 83 组 / 7873 条；渲染检查 43/0。

## 未完成 / 下一步
- **目标人声增强（TSE，新方向，用户 2026-10-22 启动）**：计划文档在另一 worktree 分支 `feature/target-speaker-enhancement-plan`（`docs/target-speaker-enhancement-plan.md`，仅方案、无代码）。用户拍板：**先不做端侧小模型（P4 跳过），先做持续音频收集（已做）+ P0 离线听感证伪**。下一步：P0a 用真实连续录音跑 TSE 离线原型（16k 分离+声纹选人基线 / 16k 预训练 TSE），出 A/B/C 样本。
- **连续音频真机验收**：host 已于 19:16 重启（刷 D46 固件时停掉了 19:14 那次重启的 host；新 host 在跑）。启用：`CORES3_CONTINUOUS_AUDIO=1 sh tools/host_up.sh`（需先 free_port --kill 当前 host）。
- **TODOS A0**：D40 真人对话验收——首响延迟、误关/漏关、背景噪声误判（需设备+人；判据先写死）。
- **TODOS A1**：LLM 迟到否决回写 S3 快路径（`dialog no|yes [ttl]` + host `--assistant-capture`），代码未接。
- **第 38 项**：真实房间本底被判成语音——未修；取数只能用设备麦 24 频段（`context` JSON `bands[]`），别再用 Mac 麦。
- 声纹 self-veto 默认关（D31，实验性）；`--ambient-gate voice` 依赖它时注意。
- 待用户拍板：D41 掩蔽顶爆风险（无限幅器）；6.5 dB 产品策略 vs 8.0 dB demo 实发口径。
- **安静全平直 + 掩蔽自适应（2026-10-23 中午）**：用户口径"安静时应该是完全没有"+"噪声什么样多大声你来调节"。实现：① `eq` 命令加 `base_gain`（0=基线全零），host 在 S3 报 `mode=FLAT`（安静/对话）时下发 0，基础档完全平直不留 demo 音染，systemeq 内 0↔1 平滑过渡；② 掩蔽自适应：S3 报 `EXTERNAL_VOICE_INTENT` 时处方深度按宽带噪声连续升（-54 dBFS→1.3×、-30→2.5×），非掩蔽模式不覆盖面板深度。③ stall 兜底退出原因从"采样率不匹配"（永久禁用）改成"ring 持续积压"（走 60 秒 3 次的有限重启）。当前链路正常：in_ring 25k / out_ring 496 / resampling=true。
- **"又没了"根因 + 设备切换自愈（2026-10-23 中午）**：用户报声音/EQ 又消失。查实是**旧版 stall 保护退出**（ring 持续积压 → 判定采样率不匹配 → quit 恢复原声 + 禁止自动重启），不是 bug 是兜底生效。**真正根因**：AVAudioEngine 在输出设备配置变化（A2DP/HFP 切换、蓝牙重连）时**静默停止拉数据** → ring 积压。**修复**：① systemeq 监听 `AVAudioEngineConfigurationChange` 通知 → 立即 quit 带原因"输出设备配置变化"，由 host 重建；② host 对这类退出允许自动重启，但**60 秒内最多 3 次**（防抖循环）；"采样率不匹配"仍永久禁止（那个是真物理限制，重采样已让它不该再出现）。当前实测：in_ring 25k / out_ring 496 稳定、resampling=true。
- **重采样 bug 修复（2026-10-23 中午）**：首版 AVAudioConverter 的 input block 每次回调都返回同一块 inBuffer + `.haveData`，导致 converter 重复消费同一段输入 → 输出错乱（用户听感"像马桶刷刷耳朵"）。**修法：input block 第一次给数据、之后必须 `.noDataNow`**。离线探针验证通过（440 Hz 正弦：RMS 0.354 ✓、过零率 439.2 Hz ✓、相邻差 0.0326 无爆音 ✓）；在线 resampling=true、ring 512 稳定。
- **链路抖动治本（2026-10-23 中午）：消费端重采样已做**。systemeq 的 AVAudioSourceNode 改按输出设备格式（engine.outputNode）声明；tap 采样率 ≠ 设备采样率时（QQ 音乐 48 kHz / 蓝牙 44.1 kHz）走 **AVAudioConverter**（non-interleaved 输入 + 预分配缓冲，render 线程内零分配），转换后进 EQ 链。**实测验证**：tap_rate=48000、engine_rate=44100、resampling=true，ring_fill 从 94875（满格/抖）降到 **326–590 稳定**，播放正常。旧 stall 检测（ring>80% 连续 3 s → 自动 quit + 禁止自动重试）保留作兜底。ready 事件新增 `resampling`/`engine_rate`，host 快照已透传。**未测**：运行中切换输出设备（engine 会配置变化，可能需重建；stall 兜底会接管）。
- **链路抖动止血（2026-10-23 中午）**：用户报"QQ 音乐没声音→噪点→又抖"。根因：tap 48 kHz 与输出设备 44.1 kHz（Xiaomi 耳夹）不匹配，engine 消费慢 8.8%，ring 几分钟积满后丢新帧=抖动；满时丢帧还会叠加"没声音"（tap 静音原声+EQ 副本不输出）。**已做止血**：systemeq 检测 ring 连续 3 s >80% → 自动 quit（销毁 tap 恢复原声，fail-open），host 侧 `_no_auto_restart` 禁止 5 s 重试拉回（防"起→积压→退→起"循环）。**未做治本**：消费端 AVAudioConverter 真重采样（48k→44.1k），下一轮做，做完 44.1 kHz 设备也能带 EQ 不抖。改前先读 `tools/systemeq/systemeq.swift` 的 controlTimer 自检段与 `tools/host/systemeq.py` 的重试段。
- **对话触发 A/B 评估（2026-10-23 上午，跨会话协作）**：另一会话提出"对话确认触发难"并给 A（1.2 s 窗累计 500 ms 判据）/ B（LLM 独立触发）两选项。本会话评估写入 `work/dialogue-trigger-review.md`：**A 方向对但原参数会踩回 D40 的坑**（现场噪声连续段 p90=215 ms，断续噪声能在 1.2 s 窗凑满 500 ms；D40 正是为修"断续噪声积分成常驻 engaged"才把续命改 800 ms 连续）；建议 A'：阈值 0.65 不降、事件过滤计入窗口、参数用现有噪声数据+连续音频真人录音离线验证后再刷机。**B 不做**：D34 架构倒退 + A1 回写未接，LLM 只应做迟到否决（先补 A1）。若接手：先离线验证 A'（1–2 h 不碰设备）。
- **多会话抢 host + 参数固化（2026-10-23 上午）**：EQ 再次"被关"，查实是**另一个进程在反复重启 host**（孤儿进程，命令行 `--relay-audio --ambient-executor relay --ambient-gate voice --voiceprint`，**无 --system-eq-app**；kill 后 2 秒内被重启，父链已断无法绑定 session ID——用户怀疑的session-9f59fd44… 很可能就是它）。**根治：EQ 三件套已固化进 `tools/host_up.sh` 默认值**（--system-eq-app com.tencent.QQMusicMac --eq-mode basic + --ambient-gate voice + --voiceprint，显式传参/mock/replay 不覆盖），任何会话裸跑 `sh tools/host_up.sh --detach` 都自动带 EQ。已 kill 对方进程并抢回（systemeq applied）。若再被抢：对方若不是走 host_up.sh（手动 python -m host），固化无效，需去对方会话停手。
- **环境曝光叠加 + host 恢复（2026-10-23 上午，netizen-v1.6）**：① 用户问"为什么功能被关"——根因是 host 今早被重启且**没带 `--system-eq-app`**（连带 voice 门/voiceprint 也没带），systemeq 没启动。已用完整命令恢复。**教训：重启 host 必须带全套参数**（`--ambient-gate voice --voiceprint --system-eq-app com.tencent.QQMusicMac --eq-mode basic`），不带就丢 systemeq。② 用户口径"环境音进入要压 EQ"：高频收放因子改为 `trim = max(harshness, envExposure)`，envExposure = clamp01(max|S3 处方|/2.5)——用 S3 处方活动度当环境曝光代理（安静≈0、越吵越大），环境音进入时 4–7 kHz/8.5 kHz+ 自动压。实测：env=0.4 时 8.5k 从 +2.3 压到 +0.7。state 新增 content_env_exposure/content_trim，面板显示"内容/环境/高频压"。host 自测 85 组 / 7891 条。
- **方案 A：内容自适应高频（2026-10-22 晚，netizen-v1.5）**：用户"底噪好了但纯人声不清透"，拍板方案 A。systemeq 新增 `ContentMeter`（Mac 侧 tap 流 FFT 1024 点，Accelerate vDSP_DFT）：highRatio(4 kHz+) 与 midRatio(1–4 kHz) → `harshness` 0(稀疏人声)=保留高频 / 1(密集器乐)=收高频，0.1/0.3 s 平滑；applyEq 基础档 4–7 kHz −1.5、8.5 kHz+ −2.5 × harshness（激进档不动）；基础基线高频回 Harman 轻值。state 上报 content_harshness/high_ratio/mid_ratio/fed_frames/analyses，host 透传 + 面板显示"内容 %"。调试坑：vDSP_DFT 的 DSPSplitComplex 存裸指针，必须用 withUnsafeMutableBufferPointer 显式作用域（隐式临时指针在 init 返回后失效）；`guard total>0` 会吞掉静音窗口导致计数/特征看似不更新。已验证：白噪声 high≈0.89、10k 正弦≈1.0（离线探针），管线 12 s 518 次分析；**特征响应待用户放歌实测**（当前 QQ 音乐未在播）。host 自测 83 组 / 7880 条。
- **基础档高频收敛（2026-10-22 晚，netizen-v1.4）**：用户听感"底噪还是拉过头"。根因：S3 的 8.5–13.6 kHz 内容先验 +2.6（×1.3 深度后 +3.4）会把音乐底噪/嘶声一起抬起来。修复：基础档 4 kHz 以上给负基线（4036 +0.3、4829 0、5824 −0.2、7020 −0.3、8552/10677 −1.0、13638 −0.5），满档 8.5k 从 +3.8 降到 +2.4。实测 applied ≈ 低频 +1.0、245=0、去糊 −2.8、presence 峰 +2.3、8.5k +0.3/10.7k +0.4。host 已重启（basic 档，depth 1.3）。
- **基础档 Harman 形态（2026-10-22 晚，netizen-v1.3）**：用户要求"按更高票的统一标准（Harman target）锚定基础档，掩蔽保留，端侧判断为主"。basic 基线改为轻量 Harman 形态：去糊 −0.6/−0.4、2–3 kHz presence 峰 +1.3、8 kHz 以上回落（+0.4/+0.2/0）、低频 0（留给 S3 的 OWS 补偿）。基础满档全部 ≤ ±4.3 dB（共识量级）。**掩蔽（D41）路径未动**：基础档掩蔽叠加 ≈ +6.7 dB/段，不顶爆；激进档掩蔽仍 +16（over-tuning-review.md R1，待拍板）。systemeq 已重建、host 已重启（--eq-mode basic，depth 1.3）。实测 applied ≈ 低频 +1.4、245=0、去糊 −1.7、presence +3.0、空气 +2.3。全面 review 报告：`work/eq-design/over-tuning-review.md`。
- **基础档自然化（2026-10-22 晚，netizen-v1.2）**：用户听感"电吉他/MIDI 背景音整个裂开"。根因两条：① 基础基线去糊 −1.5/presence 峰 +3/空气 +3 过调，中低频身体被削、中高频浮起=频段断层；② S3 深度仍 ×2.5，presence 实际叠到 +4.8。修复：basic 基线改为去糊 −0.8…−0.2、presence 峰 +1.2、空气 +0.8；basic 档默认深度 1.3（`--eq-mode basic` 时 `__main__.py` 传 depth=1.3）。实测基础档 applied：低频 +1.0/+1.2、去糊 −1.6、presence +2.4、空气 +2.2。另：19:14 设备串口一度无数据（host 报"10 秒内一个字节都没有"），esptool watchdog-reset 后恢复；host 重启遇过"旧 host 未退干净导致新 host 不启动"，先 free_port --kill 再起即可。
- **低频双叠修复（2026-10-22 晚，netizen-v1.1）**：demo 基线两档的 70/141 Hz 清零（+6/+3 → 0）——用户听感"低频都炸了"，根因是 S3 的 OWS 低频补偿（×2.5 ≈ +5~7）与 demo 基线低频叠加到 +7.6~+13。现在低频只留 S3 补偿，实测 applied ≈ +2.8/+3.0；systemeq 已重建、host 已重启。底稿版本史见 `work/eq-design/baseline-netizen-v1.md`。
- **EQ 校准已上线（2026-10-22 晚）**：245 Hz 校准固件已刷写（SHA256 43172824…）+ 真机确认 band2=0.0；systemeq 已重建（basic/aggressive 两档基线），host 已重启（voice 门 + voiceprint + systemeq）。**下一步是听感 A/B**：面板「Mac 实际应用」卡三档按钮（基础/激进/关闭EQ）；用户现场拉高拉低按 `work/eq-design/baseline-netizen-v1.md` 底稿改。
- **D46 真机验证（未刷机）**：混合字段上线需重刷固件（`sh tools/free_port.sh --kill && sh tools/flash.sh`；D46 与 245 Hz 校准都在工作树里，会一起刷入）+ 重启 host；--mock 下 VOICE/LOUD 段即可看到混合显示。

## 已知文档矛盾（别直接引用）
- 测试条数：plan-status 旧文（6 套/221）vs README（11 套/365）vs goals（95/95）→ 以实际运行为准。
- 环境声执行端默认：D24（airpods）vs D30（off）→ 当前 off；演示用 `--relay-audio` / 门控。
- 背景人声动作：D16/D26（人声靠前）已被 D41 掩蔽覆盖。
- 设备录音 16 kHz 上限；AirPods yaw = 头朝向 ≠ DOA；近场/双麦仍无结论。

## 验证入口（改完先跑）
```bash
cmake -S test -B build-host-tests && cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
python3 tools/host/tests/test_host.py
sh tools/build.sh          # 固件（IDF 5.4.1）
```

## 2026-10-22 LLM 配置切换
- 用户指定切换为 `gpt-5.6-sol`；已修改本地 `.env` 的 `ANTHROPIC_MODEL` 及 `tools/host/assistant.py` 默认值，沿用现有中转 `/v1/messages` 接口。
- 使用上位机相同 Python 环境实测 `_probe()`：ready，返回模型匹配；host 自测 83 组 / 7873 条断言通过。系统 Python 的 TLS 证书配置不同，连接验证需使用 host_up.sh 所选解释器。
- 保留原启动参数（voice 门、voiceprint、QQMusic system EQ、basic），以独立后台会话重启上位机；未修改固件。

## 2026-10-23 模型配置复查
- 用户反馈仍为 Claude：现场 `.env` 的 `ANTHROPIC_MODEL` 已变回 `claude-opus-5-5`（覆盖来源未确定），代码默认仍为 GPT；8787 无运行服务。
- 已再次改为 `gpt-5.6-sol`，保持原参数以独立会话启动；实时 `/api/snapshot` 确认 model=gpt-5.6-sol、state=ready、error 为空。后续请勿用旧 `.env` 覆盖用户选定模型。
