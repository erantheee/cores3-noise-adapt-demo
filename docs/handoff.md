# Session handoff — 2026-10-28（上午）

> 每轮收尾更新本文件（≤2.5KB）。新会话顺序：`AGENTS.md` → 本文件 → `docs/INDEX.md` → 按需读。
> 旧版交接已归档 `docs/handoff-archive-20261023.md`。

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
- **下一步（待用户听感）**：① 人声毛刺是否减轻；② 敲击掩蔽是否更弱（headroom 收缩的
  代价）；③ 若毛刺仍在，优先做"音乐漏音不当作背景人声"（EXTERNAL_VOICE_INTENT 抑制，
  需要固件+刷机）；键盘掩蔽的帧内瞬态检测（D51 后续）排在后面。
 
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
