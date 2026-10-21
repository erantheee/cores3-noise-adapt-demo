# AI OWS 耳机仓综合体验 Demo

本项目用 **CoreS3 = AI OWS 耳机仓、Mac = 播放器与原型执行器、AirPods Pro = 本次耳机**，
验证端侧 AI 根据环境、时间、个人历史和用户纠正做出的综合听音决策是否真的有用。

唯一产品口径见 [`docs/product-alignment.md`](docs/product-alignment.md)。功能、UI 和对外表述都必须与它一致。

它要回答的是综合体验：**环境判断是否可靠、歌曲 EQ 是否有益、现实人声增益意图能否执行、端到端
延迟是否可接受，以及策略是否知道何时退出或不动作。**

完整论证见 [`docs/edge-feasibility.md`](docs/edge-feasibility.md)；每条决定的原因见
[`docs/decisions.md`](docs/decisions.md)；**计划本身与每项状态见
[`docs/plan-status.md`](docs/plan-status.md)**。改行为之前先读这三份。

---

## 当前闭环

```text
CoreS3 双麦克风 → 24频段环境分析 → 对话开始预判 → 端侧策略
                                                           ↓
                                                  EarbudCommand v4
                                                           ↓ USB
                             Mac：歌曲 EQ / 媒体下压 / AirPods 聆听模式 → AirPods Pro
```

**对话感知是预判式的（D16）**：附近有人开口的那一刻就先压低媒体，确认对话成立后把
`ambient_open=true` 发给 Mac 执行端。**当前 demo 走 S3 麦克风中继**（S3 麦 → Mac → 耳机，
固有时延约 211–231 ms，先把预判与开关跑通）；后续再拓展成 AirPods 通透模式。对话结束后
恢复媒体与关闭环境声。**不是**"检测到佩戴者说话才下压"——旧实现只能事后反应，方向已改。

CoreS3 承担耳机仓侧的感知、判断与决策；Mac 承担本次 Demo 的音频执行；AirPods Pro 只播放 Mac
最终输出。AirPods 不开放双耳原始麦克风与目标人声增益接口，所以当前不能声称使用 AirPods 麦克风
完成了目标人声增强；环境声只通过切换聆听模式（通透）实现，不是目标人声提取。真实执行链路尚未
接通时，协议固定上报 `execution: decision_only`。

## 2026-10-21 收尾变更（本轮）

- **环境声执行端默认关闭（D30）**：`host_up.sh` 真机默认 `--ambient-executor off`，只显示
  `ambient_open` 意图；要环境声必须显式传 `--ambient-executor relay` 或 `airpods`。
- **声纹 self-veto（D28 第 3 步 / D31）**：Mac 侧 WavLM-SV 判定经串口回传给 S3
  （`voiceprint self` / `other` / `pending`；host 退出时发 `off`）。`self` → 不压媒体、不抬人声 EQ、不开环境声；
  `other` → 按 D16 的预判逻辑正常动作。判定 **3 秒**没有新行就自动过期，不再永久锁死；
  host 串口关闭/退出时发 `voiceprint off` 清空 S3 侧状态。`--voiceprint` 仍是**实验性门控**，
  默认演示不带，也还没有过验收。
- **固件遥测**：新增 `voiceprint_valid` / `voiceprint_self` / `voiceprint_age_ms`，
  只增键，schema 仍为 4。
- **上位机修复**：回控回执只在收到**新的** `capture` 事件时才算 `acknowledged`（缓存事件
  不能把"写成功"说成"设备照做"）；`/api/stop` 先执行清理（含 `ambient.stop`）再退出。
- 本轮未做的 P2 与已知限制见 [`TODOS.md`](TODOS.md)。

## 三笔账的当前状态

**账 1 分段延迟：端侧部分已实测。** 下列数字来自当前在跑的固件（实测日期 2026-10-14：设备在跑，
面板 `source=serial`）。控制节拍此前为 150 ms，由提交 `4308366` 改为 100 ms
（`main/app_main.cpp` 的 `kControlPeriodMs`）。

| 阶段 | p50 | 说明 |
|---|---|---|
| 采集间隔 | **100.03 ms** | 设备自报 `capture`，就是控制节拍，目标 100 ms |
| 24 频段分析 | **7.02 ms** | 占节拍的约 7% |
| 声纹推理 | 0.0 ms | **仍未实现**，如实报 0（`embed` 阶段） |
| 决策 | **0.06 ms** | 与打点开销同量级 |
| 打点自身开销 | 1.52 µs | 两次读时钟 |

融合第二轮之后，端侧多了一次**真实的**前向：听音情境模型 v0.2-s3（MLP 92-32-16-9、
3,657 参数、3,600 MACs）。它随 `latency` 报文单独上报（2026-10-16，45.0 秒连续实测）：

| 项 | 值 | 出处 |
|---|---|---|
| 模型前向 p50 | **151–158 µs** | `latency` 的 `context_p50_us` |
| 模型前向 p95 | **161–180 µs** | `context_p95_us` |
| 模型前向 max | **182 µs**（首轮 681 µs，含冷启动） | `context_max_us` |
| 帧到达速率 | **10.02 帧/秒**（451 帧 / 45.0 秒） | 主机侧计数；设备节拍就是 10 Hz |
| 一帧 JSON | **p50 744 B → 约 7.5 kB/s** | 口径更正见 `plan-status.md` 第 20 项 |
| 音频任务剩余栈 | **4,872 B**（历史最低水位） | `latency` 的 `stack_high_water` |

主机侧到达间隔 p50 = **100.00 ms**（这是"上位机有没有跟上"的自检，不是设备节拍）。

**端侧本机不是瓶颈：分析 7.0 ms + 决策 0.06 ms + 模型 0.16 ms ≈ 7.2 ms，100 ms 节拍下余量约 93%。**
剩下的是音频通路本身（蓝牙 311 ms），那一段与端侧算力无关。

**"端侧 AI 很快"这句话现在的边界**：模型这一笔是实测的，但它是个 3,657 参数的小网，这个数字
**不能**外推到声纹或端侧目标提取（第 14 项，6 GMACs 量级，是另一个工程）。声纹推理仍然报 0，
那是因为没有实现，不是因为免费。

| # | 待回答 | 状态 | 卡在哪 |
|---|---|---|---|
| 1 | 一小时连续运行的延迟漂移 | 未测 | 需设备在场跑 `tools/drift_capture.py` |
| 2 | 近场拾音信噪比 vs Mac 的 **0.9 dB** | 未测 | 需录三类音频；双麦是否可用也待验 |
| 3 | 判据阈值用真实录音标定 | 未做 | 需录安静本底/白噪声/纯音/人声 |
| 4 | 声纹可分性（类间间距 vs 类内抖动） | ✅ **已过止损门**（D28） | WavLM-SV 间距/抖动 **6.41**、EER 0%；素材与代理边界见 D27/D28 |

第 4 项带**止损门**：间距/抖动 ≥ 3 且 EER ≤ 5% 才继续，否则停止声纹路线。工具
`tools/voice_separability.py` 已就位且判据写死。2026-10-19 用 WavLM-SV 后端通过
（**6.41 / EER 0%**），声纹据此进入实验性门控；端侧推理仍未实现，Mac 侧 self/other 判定
与 S3 门控见下文「2026-10-21 收尾变更」。理由见 [`docs/decisions.md` D7](docs/decisions.md)。

## 真机踩到的坑（换硬件前请先读）

下面每一条都**只在真机上暴露**，桌面机上完全看不见，写在这里是为了不要重犯：

1. **桌面基准会骗人。** 把 24 频段分析里的 Hann 窗提出循环外，真机上 57.1 ms → 7.0 ms
   （8 倍），桌面上只有 14%——因为桌面编译器会向量化那段循环，ESP32-S3 不会。
2. **控制节拍超时的真因不是日志。**（这一段是当时的记录，节拍目标后来由 150 ms 改为 100 ms，
   但这条推理与目标值无关。）周期比目标长出几十毫秒，第一反应是串口阻塞，于是把波特率从
   115200 提到 921600：**周期一点没变，假设被推翻**。真因是读取粒度——节拍判断只能在一次
   读返回后执行，永远跨过截止点，平均超出半个读取块。减小每次读取的样本数后，半块过冲仍在，
   只是回到了测量分辨率附近。
3. **"打点开销"很容易测成"被测对象"。** 这条度量我写错了三次，前两版分别报 57 ms 和
   7.1 ms，**都是把分析本身算了进去**，因为结束时刻的读时钟放在了工作之后而不是紧挨着开始。
   现在相邻两次读时钟，1.52 µs。
4. **刷机打印 "Hash of data verified" 不等于写进去了。** 2026-10-19 用 esptool v5.5.0 把
   bootloader、分区表、app 三个"地址 文件"对放进同一条 `write-flash`：日志逐区打印、每区都报
   verified，但回读 `0x8000` 全是 `0xFF`、`0x10000` 还是上一版固件。启动停在
   `E boot: No bootable app partitions`。`tools/flash.sh` 已改成**逐区写、逐区校验**。
5. **USB-JTAG 的"硬复位"不一定真复位。** 刷完 `Hard resetting via RTS pin...` 之后设备可能
   停在下载模式，端口在、但一个字节都没有（本项目已知"节点在≠设备在"）。用
   `esptool --after watchdog-reset run` 才能把 app 跑起来；再不行就拔插 USB。
6. **刷机/读串口要用 `danger-full-access`。** 工作区沙箱默认禁止打开 `/dev/cu.usbmodem*`，
   表现为 `Errno 1: Operation not permitted`——它不是"端口被占用"。

## 屏幕（中文界面）

- 第一页是 S3 环境与决策；左滑进入第二页查看 Mac→AirPods Pro 路由
- `环境`：安静 / 噪声 / 人声嘈杂 / 高噪声
- `当前动作`：保持 / 歌曲调音 / 现实人声增益意图 / 安全保持
- `人声状态`：`NONE / SPEECH / LIKELY`；只是 S3 麦克风启发式，不是身份识别
- `调音`：`SONG_EQ` 的 24 段建议；现实人声模式下保持歌曲 EQ 平直
- `人声增益`：现实中对方说话的增益意图，硬上限 +6.5 dB
- `控制链路`：Mac 串口是否真实连接
- 第二页显示 `AirPods Pro`、Mac 控制通道和 Mac 回报的蓝牙输出状态
- 底部信息行：麦克风正常/异常、处理耗时与剩余内存
- 底部那行右侧：声音事件类别（ASCII 的 `keyboard / wind / speech / artifact / quiet`，
  窗口没攒满时显示 `--`）；事件同时进串口与上位机面板
- `感知`：端侧听音情境模型的场景类别，把「固件没报 / 窗没满 / 有判断」三种状态分开显示
- 屏幕字库是 115 字 CJK 子集，加任何新标题都要先过 `tools/render_demo_ui.py` 的缺字检查

开机不播放任何歌曲，内置扬声器保持静音。

## EarbudCommand v4

每约 100 ms 从 USB 串口输出一帧 JSON（`kControlPeriodMs = 100.0f`，`main/app_main.cpp`；
录音期间放宽到 250 ms，见同文件的 `kCapturePeriodMs`。上位机启动时也如实打印这一行）：

```json
{"type":"earbud_command","schema":4,"sequence":42,"timestamp_ms":6300,"speech_gain_db":0.00,"external_voice_gain_db":0.00,"media_percent":60,"ambient_open":true,"state":"ADJUSTING","environment":"VOICE","mode":"FLAT","conversation":"LIKELY","confidence":0.92,"execution":"decision_only","link":"simulated","eq_gain_db":[0.0,...,0.0],"ambient_band_dbfs":[-52.1,...,-61.0],"event":"quiet","event_valid":true}
```

版本历史：v2 = 调音与外部人声字段分离；v3 = 自身语音证据（`own_voice_*` / `imu_*`）；
v4 = `ambient_open`（环境声开放意图）。`event` / `event_valid`（端侧声音事件判断）是
**v4 上追加的键**，不涨 schema——与 `latency` 报文追加字段同一条理由：上位机按名读取，
只增键不破坏旧解析。上位机同时读得懂 v1–v4，旧帧缺字段一律报 `null`，不猜默认值。

**`ambient_open` 是意图，不是执行回执。** S3 决定"对话成立，应该让现实声音进来"，执行端
由 Mac 选：`--ambient-executor relay`（S3 麦克风中继，S3 说开才放行音频包）或
`--ambient-executor airpods`（兄弟工程 `airpods-control` 切通透模式，关闭切回原模式）。
**`host_up.sh` 真机模式默认 `--ambient-executor off`（D30）**：默认只显示意图、不执行；
要环境声必须显式传 `relay` 或 `airpods`。执行结果、失败原因、放行/丢弃包数在面板
「环境声」卡上单独显示；没有执行端时意图仍然可见，不允许把意图写成已执行。

没有 Mac 执行回执前，`execution` 必须保持 `decision_only`。`eq_gain_db` 按模式区分（D26）：
**对话成立 / 声纹判定 self** 时保持平直（下压媒体、不抬歌曲中频）；**背景或别人说话但还没构成
对话**（`EXTERNAL_VOICE_INTENT`）允许抬 8–16 段（约 1–3.6 kHz）作为代理；**`SONG_EQ`** 按
环境谱写稳态噪声 EQ；风时低频抬升压到 1/4。不能把"抬歌曲中频"写成"现实人声已增强"。

`eq_gain_db` 是 S3 给 Mac 的歌曲 EQ 建议，`ambient_band_dbfs` 是
S3 实测环境谱。**Mac 目前只按 S3 的逐段建议 × 深度倍率执行，没有做歌曲内容分析**——"结合歌曲
内容生成最终处方"是计划，不是现状；面板上必须按这个口径显示。`external_voice_gain_db` 是现实
人声增益意图，不代表 AirPods 已执行。

v4 还带判据遥测：`broadband_dbfs` / `voice_likelihood` / `vocal_ratio` / `spectral_flatness` /
`vocal_peakiness` / `excited_vocal_bands`；2026-10-21 又追加了声纹回显
`voiceprint_valid` / `voiceprint_self` / `voiceprint_age_ms`（只增键，schema 仍 4）。
前六个只上报、不参与决策，是对话预判训练与误触发排查的数据；后三个是 S3 实际采信的声纹
判定回显，供面板与排查读（见 [docs/conversation-prediction.md](docs/conversation-prediction.md)）。

`noise_reduction_db` 是**降噪量的大小**，取值 0…+8，不是负值偏移；它只存在于旧固件。

**本机不做降噪**（2026-10-14 用户明确口径）：动作只有**增益与调音**。这个字段曾是固件里的
残留——控制器算了它、设备屏幕把它列成「降噪」、JSON 也照发，而它在整条链路里没有任何
消费者。**2026-10-14 已从固件删除**：`EarbudCommand` 里没有这个字段了，设备屏幕上那一格
改成「调音」，JSON 也不再发它。上位机的解析层仍接受它，只为读得懂升级前的历史日志。

## 两个增益上限：产品策略 6.5 dB 与 demo 实发 8.0 dB

`main/controller.hpp` 里有两个容易被当成同一个的上限，它们不是一件事，改之前要分清：

| 量 | 值 | 出处 | 性质 |
|---|---|---|---|
| `kMaxSpeechGainDb` | **+6.5 dB** | `main/controller.hpp` | **产品策略**上限：真实产品要"不惊到听者" |
| `kDemoSpeechGainDb` | **+8.0 dB** | `main/controller.hpp` | **demo 实发**上限：本 Demo 要"看得见" |

这两个上限管的是**幅度**，到得多快由 `main/controller.cpp` 的斜坡决定：人声增益攻击 **120 ms**、
释放 **600 ms**（`slew` 是指数式，到目标 90% 约 **300 ms**）；媒体让位的深度斜坡是 **250 ms**。

**实际下发的是 8.0**：`main/controller.cpp` 末尾是
`command.speech_gain_db = std::clamp(speech_gain_db_, 0.0f, kDemoSpeechGainDb);`，媒体音量地板同理用
`kDemoMediaFloorPercent = 60`。60% 是当前温和对话下压值；此前的 25% 是强调演示效果的强下压，
并非模型推理结果。8.0 dB 也不是随手挑
的——它落在姊妹 OWS 项目实测的语音带 +7.8 dB 之内并留有余量，且单测要求任何一帧都不得超过项目
既定的 +10 dB 天花板（`test/controller_test.cpp`）。

**对读数的要求**：面板那行「硬上限 +6.5 dB」与快照里的 `caps.speech_gain_db = 6.5`
（`tools/host/protocol.py` 的 `MAX_SPEECH_GAIN_DB`）指的是**产品策略值**，不是当前固件的实发上限。
按 6.5 去校验帧，8.0 dB 的正常帧会被标成「超出硬上限」的异常——这个不一致在上位机侧，本文档只
记录事实与出处，不改数值。

## 人声判据

判据用四个描述量相乘：语音带能量占比、语音带峰值集中度、**频谱平坦度**、**被激发的频段数**，
再乘电平门。

修复前的实测缺陷与修复后的对比：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| 白噪声 −45…−25 dBFS | 判为**人声嘈杂**，媒体音量被压到 73–89% | 非人声，媒体音量保持 100% |
| 1 kHz 纯音 −30 dBFS | `voice_likelihood = 1.00` | 非人声 |
| 500 Hz / 2 kHz 纯音 | 判为人声 | 非人声 |
| 削波信号 | `mic_valid = true` | 麦克风失效 |

**已知边界**：极窄带的高集中度语音仍可能被拒绝。阈值常量在 `controller.cpp` 的
`voice_thresholds` 与 `clip_thresholds` 命名空间里，**必须用真实录音重新标定**——合成信号
只能证明"已知假阳性没了"，不能证明准确率。

### 对话触发：预判式（D16）

判据回答的是"像不像人声"；**触发对话感知的不是它**。总目标是"预判我即将跟别人讲话"，所以：

- 触发输入是 `conversation_cue`：附近有人开口 + 近场电平门（-58…-42 dBFS）。第一拍进入
  `anticipating`，媒体立刻预下压（70%）；**不依赖佩戴者开口**。
- 证据持续 200 ms 进入 `engaged`：媒体压到 60%，同时把 `ambient_open=true` 发给 Mac。
- 最后一个人声后 2.5 s 无声 → 恢复媒体、关闭环境声；短促声退回 `idle`，环境声不为咳嗽切换。
- 旧的"自身语音是唯一触发输入"已删除（它只能事后反应）；`own_voice` 只保留为可选确认。

代价写在 D16：播客、电视、旁边桌说话都可能触发预判；当前没有方向与身份信息，靠轮次归属
（状态表第 36 项）或声纹收紧。验证脚本的旧 C9 断言与新目标冲突，需按 D16 重写
（本轮未做，见 [`TODOS.md`](TODOS.md)）。

### 声音事件判断（键盘 / 风 / 撞击）

9 类情境模型没有 keyboard 类，且真实风判不出来；所以另做一条 **1 秒窗的规则判断**
（分类只上报，但 keyboard / wind / artifact 会作为对话与环境声的**否决条件**，见 D27；
风还会驱动风 EQ，见 D21），代码在 `main/sound_event.hpp`，现状与数字见
[`experiments/sound-events/README.md`](experiments/sound-events/README.md)：

- 输入仍是固件已有的 24 段 dBFS + 宽带电平，**不新增 DSP**；15 维特征（质心、斜率、
  语音带峰值集中度、低频能量占比、包络起伏……）全部是它们的初等函数。真机实测一次判断
  **p50 269 µs / p95 276 µs / max 319 µs**（`latency` 报文的 `event_p50_us`，2026-10-19），
  占 100 ms 节拍的 0.27%。
- 输出 `event`：`quiet / speech / keyboard / wind / artifact`，随串口帧下发，面板新增
  「事件（端侧规则）」卡。`event_valid=false` 表示 1 s 窗没攒满或刚被撞击清空——界面必须把它
  与"判断为安静"分开，否则就是在把"还没判断"说成"判断为安静"。
- **撞击是前置门控**：削波帧（`mic_valid=false`）直接清空事件窗并报 `artifact`。键盘与撞击
  在时域上都是冲击，不先门控的话撞一下桌子就会被判成敲键盘。
- 2026-10-19 两段真机采集上：keyboard/wind 召回 **83.7%**（keyboard 78% / wind 90%），
  安静/说话误报 **0.0%**。**这是单键盘单房间的数字，不是跨条件泛化证据。**
- **真机固件复测（v5b）**：键盘段 79% 帧报 keyboard（连续 15 秒不闪断）；风段 **0 帧**
  被误报成 keyboard（加"斜率不能太陡"门前是 20%）；敲桌段削波时报 artifact，未削波时
  落 quiet，只有 5/60 帧报 keyboard。**口径：单次现场行为验证，不是准确率测量。**
- 已知假阳性方向（必须记住）：这版规则不区分"键盘"与"其他未削波的瞬态"（关门、杯子碰桌），
  负样本还没采；9 类模型把真实说话 100% 判成 `other`（域偏移），与事件规则无关。

真正修"键盘被四判据当成人声、导致媒体下压"（本次采集实测键盘帧 82.6% 报 `VOICE`、
80.6% 进 `conversation=LIKELY`、媒体被压到 60%）需要让**决策路径**消费事件判断或收紧
四判据；本轮按"只上报、不改决策"落地，行为变更要单独拍板。

## 构建

本地要求 ESP-IDF 5.4.1：

```bash
idf.py set-target esp32s3
idf.py build
```

云端（无本地 IDF 时）由 GitHub Actions 构建。CI **先跑主机单测再构建固件**，单测不过不会出固件。

刷写前必须确认目标是 CoreS3，并保留完整 16 MB 原始固件备份（本机备份位于 `outputs/`）：

```bash
idf.py -p /dev/cu.usbmodemXXX flash monitor
```

## 主机单元测试

`main/controller.cpp` 刻意不依赖 ESP-IDF，所以整条决策路径可以在开发机上秒级验证。
**新增判据或阈值时必须在这里补用例。**

当前 **11 个套件、365 条断言**（2026-10-21 定稿实测全部通过；明细：controller 80、
noise_analyzer 46、latency_profiler 40、mic_capture 16、mic_decimator 15、voiceprint_gate 60、
context_window 23、conversation_awareness 38、bone_voice 19、sound_event 25、speech_gate 3）。

`context_window` 是融合第二轮加的：它拿 `experiments/listening-model/scripts/features.py`
（训练那一侧的权威实现）在固定配方上的输出当金标准，逐维比对固件里的 92 维窗口构造。
移植错一个索引不会报错，只会安静地换个类别——所以这条比对必须存在。
重新生成金标准：`.venv/bin/python experiments/listening-model/scripts/emit_window_golden.py`。

`sound_event` 是声音事件判断加的：它用同一份真机采集导出 15 维窗特征与规则判断当金标准
（`test/sound_event_golden.h`），逐值比对 C++ 移植；同时用合成序列单测地板跟踪器、
状态机保持、撞击清窗。重新生成：
`.venv/bin/python experiments/sound-events/export_golden.py --out test/sound_event_golden.h <两段 CSV>`。

```bash
cmake -S test -B build-host-tests
cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
```

CI 里这一步跑在固件构建**之前**（[`.github/workflows/build.yml`](.github/workflows/build.yml)），
单测不过不会出固件。

上位机（`tools/host/`）另有一套自己的自测，同样是纯 Python、秒级、不需要设备：

```bash
python3 tools/host/tests/test_host.py            # 57 组、7594 条断言（连跑 3 次稳定）
node tools/host/tests/render_check.js <快照.json> # 可选：用 DOM 桩真跑一遍界面渲染（49 条通过、0 条失败，条数随快照内容略变）
```

`render_check.js` 存在的原因很具体：这台机器上无头浏览器拿不到显示器，系统也不允许截屏，
所以"打开看一眼"这条路是断的。它把 DOM 换成桩、喂进一份真实快照
（`curl -s http://127.0.0.1:8787/api/snapshot > /tmp/s.json`），断言 render() 到底写了什么。

CI 还会跑检查器自测 `tools/verify_conversation_awareness.py --selftest`：八个合成场景，
**8/8 符合预期**。

没有 cmake 时可以直接编译（等价）：

```bash
c++ -std=c++17 -I main test/controller_test.cpp main/controller.cpp -o /tmp/controller_test && /tmp/controller_test
c++ -std=c++17 -I main test/noise_analyzer_test.cpp main/controller.cpp -o /tmp/noise_test && /tmp/noise_test
```

## 端侧录音

声纹可分性、近场信噪比、判据标定这三笔账**都必须用设备自己听到的音频**。笔记本录的是笔记本
的声学，对端侧结论无效。固件因此内置了一条录音通路：

```bash
# 1) 用 921600 打开串口（16 kHz 录音在 115200 下会丢块）
CORES3_BAUD=921600 sh tools/monitor.sh   # 它是 shell 脚本，不是 python；默认 115200

# 2) 让设备开始录，然后录音脚本落盘
#    在串口里发命令：rec start 10     （录 10 秒）
#                    rec start        （一直录，直到 rec stop）
#                    rec stop
python3 tools/record_from_device.py --port /dev/cu.usbmodem1101 \
    --baud 921600 --seconds 12 --out 记录/甲-安静.wav
```

设备每 64 ms 发一行 base64 分块（16 kHz 单声道）。**为什么是 16 kHz**：48 kHz 单声道
base64 约 128 kB/s，而串口可用吞吐约 92 kB/s（139%，不可用）；16 kHz 占 46%。

录音脚本会**报告丢块**。丢块会让声纹类内抖动与信噪比失真，所以有丢块的录音不能用于结论，
脚本遇到丢块返回非零退出码。

### 录什么

| 用途 | 怎么录 |
|---|---|
| 判据标定 | 安静房间本底、白噪声、1 kHz 纯音、真人语音，各 10 秒 |
| 近场信噪比 | 设备拿在手里对 10–20 cm 说话、1 m 外说话、嘈杂本底，各 30 秒 |
| 声纹可分性 | **同一人分两次录**（换距离、换音量）至少 3 条；另一人 3 条 |

声纹那组一定要**刻意改变条件**——只在一个条件下录出来的"稳定"声纹，在真实使用中不成立。

## 长窗口漂移采集

计划里的"一小时漂移"由这个工具采集并汇总。它比较**首尾各 25%** 而不是整体平均——平均值正好会
把要找的漂移抹掉。

```bash
# 采集（脚本需要 pyserial；macOS 自带 python3 通常没有）
# 把下面的 python3 换成你本机带 pyserial 的解释器，或先 pip install pyserial
python3 tools/drift_capture.py --port /dev/cu.usbmodem1101 --seconds 3600 --out 记录/drift.jsonl

# 汇总
python3 tools/drift_capture.py --summarise 记录/drift.jsonl
```

## 声纹可分性测量

在投入端侧声纹工程**之前**先跑这个：

```bash
python3 tools/voice_separability.py --enroll 甲A.wav 甲B.wav 甲C.wav \
                                    --impostor 乙A.wav 乙B.wav 乙C.wav
```

需要同一人分次录制的多条录音（**故意改变距离与音量**）与另一人的录音。工具会在依赖未就位时
明确报缺（退出码 3），不会给出假结果。

## 上位机（本仓库自带，不依赖 ows-earbud-demo）

一个本地面板：把设备每 100 ms 输出的那一帧变成看得见的数字，并可以回控设备录音。
**它是本仓库自带的**，不改动你已排练的 `ows-earbud-demo` Swift 工程。

```bash
sh tools/host_up.sh                    # 真机（自动找 /dev/cu.usbmodem*）
sh tools/host_up.sh --relay-audio      # S3 麦克风实时中继到 AirPods（可 --relay-device 改目标）
sh tools/host_up.sh --ambient-executor relay  # S3 的 ambient_open 意图 → S3 麦克风中继到耳机（当前玩法）
sh tools/host_up.sh --ambient-executor airpods  # 后续：切 AirPods 通透模式
sh tools/host_up.sh --mock             # 没有设备也能先把界面跑起来
sh tools/host_up.sh --replay 记录/x.log --speed 4    # 回放历史日志
# 然后打开 http://127.0.0.1:8787/
```

`--relay-audio` 会自动让设备进入无限时长采集，把 16 kHz 单声道 PCM 送给 Mac，再由 Mac
播放到**指定设备**（默认 `AirPods Pro`，`--relay-device` 可改）。固件以 20 ms 小包发送。
中继期间同一份麦克风流仍进入环境分析，所以 VAD、歌曲 EQ 和现实人声意图不会因为开始中继而
冻结。

**播放端是 `tools/relay/relaysink`，不是 ffplay。** 换掉它的唯一理由是**延迟必须可测**：

| | ffplay（旧） | relaysink（现在） |
|---|---|---|
| 排队深度 | 测不到：不反压、不暴露播放位置 | **排队 = 已交付 − 播放器回报已播完** |
| 实测（`tools/probe_player_buffer.py`） | 按实时速率喂 5 秒音频，**0.94 秒吞完（5.3×）** | 无节流时立刻按上限丢弃（`dropped_ms`） |
| 播放到哪 | 跟随系统默认输出（跳到扬声器也不说） | 用 AUHAL 钉住名字匹配的设备，**不改系统默认输出** |
| 延迟上限 | 靠上位机的虚拟时钟猜（100 ms，猜错就永久留着） | `--relay-target-ms`（默认 60 ms），超限丢**来不及播的那一块** |

**实测（2026-10-15，固件 20 ms 分包，AirPods Pro，设备连续中继 50 秒）：**

| 项 | 值 | 出处 |
|---|---|---|
| 播放端排队 | **40–60 ms**（上限 60） | 面板「中继延迟」卡（`queued_ms`） |
| 驱动侧输出（蓝牙） | **170.7 ms** | 同一张卡（`driver_ms`：回调缓冲 512 帧 10.7 + 设备延迟 7680 帧 160.0） |
| **合计：进播放端 → 耳机出声** | **约 211–231 ms** | `est_out_ms` |
| 设备侧打包 + USB | 约 20 ms + 未单独测 | 固件 320 点一块（设备自报 `decimated/chunks = 320.1`） |
| 丢弃 | **0 ms / 50 秒**；启动排空 0 ms | `dropped_ms` / `drained_ms` |

**这个 211–231 ms 是这条拓扑的物理下限，不是调参调出来的**：其中 170.7 ms 是蓝牙输出，
`relaysink` 只占 40–60 ms，设备侧 20 ms。要更小只能换拓扑（见 D2：混合必须发生在出声的那台
设备上）。面板上这一格同时写着"不含设备侧打包与 USB"，不许把它当成整条链路的全部。

**不许把「测不到」显示成「延迟为 0」**：`--relay-player ffplay` 那条路保留着只为 A/B 对比，
面板对它会写明"测不到"。

这一版是**未经增强的现实声音中继**：它用于先测通路和总延迟，还没有完成声纹排除、目标人声
提取、方向选择或歌曲避让 EQ。不要把听到现实声音写成“已经完成目标人声增强”。

面板会显示：环境 / 当前动作 / **语音段增益** / **调音（24 频段 EQ 形状）** /
**环境声（S3 意图 vs Mac 执行结果）** / **中继延迟（实测排队 + 驱动侧输出）** /
**决策模式**（设备上报的 `mode`：`FLAT` / `SONG_EQ` / `EXTERNAL_VOICE_INTENT` / `SAFETY_HOLD`）/
控制链路 / 置信度、最近 90 秒的趋势、端侧分段延迟（每 10 秒一次汇总）、状态变化与异常列表、
以及最近几行设备原始输出。

那一格**不再是「建议媒体音量」**——提交 `eb53704` 已把这张卡片改成「决策模式」。媒体音量现在只
出现在趋势图的图例里，写作「媒体音量 %（当前固定100）」，不是一格主读数。

**面板上没有"降噪"**：本机的动作只有增益与调音，所以那一格、图例里的那条曲线都删掉了。

**四条口径写死在界面上，不靠人记：**

| 界面上的东西 | 口径 |
|---|---|
| 控制链路 | 设备报 `simulated` 就显示 `模拟`；没有确认回执前不得写成已连接或已发送 |
| `n = 0` 的延迟阶段 | 显示「未实现」。声纹推理报 0 是因为没实现，**不是因为它免费** |
| 序号跳变 | 趋势图上画一条红色竖线，异常列表里写明丢了几帧；曲线不会替设备"连过去" |
| 数据年龄 | 端口开着但没帧时显示「已连接（无数据）」并告警——节点在 ≠ 设备在 |

**回控是白名单，不是串口终端。** 界面回控只允许发 `rec start [秒数]` 与 `rec stop`。
设备控制台本身另有 `route ...` 与 `voiceprint ...` 等内部命令（见 `main/app_main.cpp`），
但界面不会转发任意输入，也不做"顺手转发"。回执只在收到**新的** `capture` 事件时才算
`acknowledged`——写成功不等于设备照做。在 `--mock` / `--replay` 下没有设备可写，回控会明确返回拒绝。

### 录音按钮曾经用不了（已修，留作复盘）

2026-10-14 实测：命令能写进串口（`write()` 成功、macOS 侧 `out_waiting = 0`、tty 标志
正常），但**设备没有任何反应**——不会出现 `{"type":"capture","state":"started"}`，
`mic_chunk` 一个都不来。所以当时按钮返回的是这样一条：

```
{"ok": false, "acknowledged": false,
 "warning": "命令 rec start 10 已写入串口，但 2.0 秒内设备没有任何回应：它没有进入录音态……"}
```

**为什么这是对的行为**：写成功不等于设备照做。旧版会回一个 `ok:true`，于是界面上看起来
"点过了"，而设备什么都没干——那正是最糟的一种谎。

**最可能的原因（待验证）**：固件的主控制台是 UART0（`sdkconfig.defaults` 里没有任何
`CONFIG_ESP_CONSOLE_*`），而 `poll_console_command()` 用的是 `std::getchar()`。在这块板上
输出确实走 USB-Serial/JTAG（能看到 `ESP-ROM` 横幅与 JSON），但**输入未必跟着走**——
如果 stdin 绑在 UART0，那 USB 上写进去的字节就没人读，症状正好是"写得进、收不到"。

**已修并真机验证**（固件 `26789ad`）：10 秒录音产出 10.03 秒音频、157 块、丢块 0。
除控制台之外，这条路上还修了 5 个缺陷（抽取器容量差 1 个样本导致录音全空、录音数据耦合进
分析窗口、录音期间分析抢缓冲、读取与块大小绑定、读取循环因缓冲满空转），全部由设备内计数器
定位。设备内诊断（`state:"progress"`）现在每 5 秒走一次串口并显示在面板「设备录音」区。

**当初的修法**：把控制台 I/O 从 UART0 改为内置 USB-Serial/JTAG，
让 **stdin 和 stdout 走同一条链路**：

```
sdkconfig.defaults:
  CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y      # 主控制台 = 内置 USB-Serial/JTAG
  CONFIG_ESP_CONSOLE_SECONDARY_NONE=y       # 次要控制台：NONE（v5.4.1 没有 UART0 选项）
```

> 注意：ESP-IDF v5.4.1 的 `CONFIG_ESP_CONSOLE_SECONDARY_*` 只有 `NONE` 与 `USB_SERIAL_JTAG`
> 两个选择，**没有** `CONFIG_ESP_CONSOLE_SECONDARY_UART0`；写未知键不会报错，会被静默忽略。
> 因此接串口适配器**看不到**日志（不是"还能看"）。

**代价**：设备用充电器供电（不接电脑）时就没有控制台输出了。对这台台架设备可以接受——
另一个选择是它永远收不到命令。

**面板里的录音块不会自动落盘。** 落盘仍然由 `tools/record_from_device.py` 负责，
因为它同时报告丢块；两套写盘逻辑各记一半比不记更糟。

### 端口被占用时它会自己往前走

默认 8787。被占用时**不会退出**，而是向后找端口并如实打印实际地址——现场最常见的占用
原因是上一次没退干净的上位机（本次实测就是：`ows-earbud-demo` 的 `demo` 还在跑，占着
8787，所以上位机落在 8788）。

### Mac 播放与执行端（播原版 + 实时调音）

**S3 决策，Mac 执行。** 面板上的「播放（Mac · 原版）」播的是本地曲目**原文件**，不做任何
预渲染；S3 每 100 ms 报一次环境与 24 段建议，Mac 把这些频段**实时**应用到正在播放的音频上。

```bash
sh tools/player/build.sh          # 用 swiftc 编出 tools/player/player（本机无 Xcode 全量，CLT 够）
sh tools/relay/build.sh           # 用 swiftc 编出中继播放端 tools/relay/relaysink（--relay-audio 需要它）
sh tools/host_up.sh               # 起上位机时会自动带起播放器（--no-player 可关）
```

- 曲库：`tools/player/tracks.json`。**先按两首做**（《渡口》与 Sweet Child），加第三首只需往
  `tracks` 里加一项，播放器与面板都不用改。
- **默认播放增益 150%**：100% 是文件原始电平，网页可在 0–200% 调节；它与对话媒体衰减分别计算。
- 为什么是独立的 Swift 进程：macOS 上能做实时多段 EQ 的只有 `AVAudioEngine + AVAudioUnitEQ`，
  而 Python 这边没有 PyObjC。协议是 stdin/stdout 上的 JSON 行，见
  `tools/player/player.swift` 顶部注释。
- 24 段中心频率与固件 `main/controller.cpp:8` 的 `kBandCenter` **逐段一致**；带宽用 0.30
  倍频程近似（固件那 24 段是分析滤波器组，Mac 这里是参数均衡）。

**面板上那两条曲线不是一回事，不许混着读**：

| 曲线 | 含义 |
|---|---|
| 蓝线（调音卡片） | **S3 报的原始建议**（24 段由设备实测下发；只有缺 `eq_gain_db` 的旧固件才标"推导"） |
| 绿柱（Mac 实际应用卡片） | **Mac 真正设进 EQ 的值** = S3 的逐段形状 × 处方深度（默认 2.5×，夹到 +10 dB） |

形状永远来自 S3；**深度倍率是本机的演示参数，不是设备的建议**，面板上写明了这一点。
产品策略上限是 +6.5 dB（`kMaxSpeechGainDb`），固件实发上限是 +8.0 dB（`kDemoSpeechGainDb`），
Mac 侧再放大也不越过用户既定规则的 +10 dB。

**回执分三件事写，不许合成一句**：设备自报 `execution=decision_only`（它自己的视角）、
Mac 的 `applied` / `idle` / `silent` / `bypass` / `unavailable`（有证据：
24 段已设 + 引擎在跑 + 最近 2.5 秒实测有信号）、以及 **AirPods 是否已播放：无法验证**
（这条链路没有回执通路）。没在播放时回执就是 `idle`——决策已产生但没人执行，
面板必须能看出这个差别。

**踩过的两个坑（都已修，别再犯）**：

1. **`eq` 一开始不在命令白名单里**：`apply_decision` 每 100 ms 抛 `PlayerError` 又被
   `except` 吞掉，现象是**面板全绿、两条曲线恒为 0**。现在失败原因会留在快照的
   `player.last_error` 里显示出来，并有单测钉住。`eq` 只允许上位机发——面板只能调深度，
   不能自己造曲线，否则"谁在做决策"这个唯一变量就没了。
2. **用瞬时峰值判"没出声"会误判**：真实歌曲有安静段（《渡口》开头就掉到 −120 dB），
   正常播放会被报成 `silent`。现在看**最近 2.5 秒窗口内最大值**。
3. **+10 dB 的处方会顶爆——这是有意的。** 实测（同一首、同一 30% 音量）：平直峰值
   −10.6 dB，抬起 9 个语音段 +10 dB 后到 **+3.07 dB**，即削波约 3 dB。试过在 EQ 后串一级
   Apple PeakLimiter：峰值确实压住了（−11.4 dB），但**中位抬升从 11.8 dB 掉到 3.9 dB**，
   "明显"被削掉大半。**2026-10-15 用户定的口径：demo 要的是明显，不是好听**，所以保留全量
   处方、不加限幅。面板照旧报峰值——**正数就代表顶爆**，看得见，但不自动压。

### 上位机自测

```bash
python3 tools/host/tests/test_host.py      # 57 组、7594 条断言，不需要设备、不需要串口
node tools/host/tests/render_check.js <快照.json>   # 用 DOM 桩真跑一遍 render()：49 条通过、0 条失败（条数随快照内容略变）
```

它自带一份真机日志 `tools/host/tests/fixtures/device_real.log`（**2026-10-15 从当前固件重抓**：
100 ms 节拍、schema 2、带 24 段 `eq_gain_db`；41 帧连续原文 + 1 条 latency 汇总，对应设备运行
约 703.1–707.1 s，不是整段会话）。**换固件后要重新抓一份，而不是改测试里的期望值去迁就。**
当前固件已经是 schema 4，这份 v2 夹具重抓已列入 [`TODOS.md`](TODOS.md)（本轮不动）。
解析器以后被改动时，必须仍然读得懂这台设备真正吐出来的字节，而不是只读得懂测试里手写的 JSON。
旧固件（schema 1、只有 `noise_reduction_db`）的帧另用手写样本测，见
`test_old_firmware_frame_still_reads`。

### 一个必须分清的口径：主机侧帧到达间隔 ≠ 设备节拍

面板「统计」里那三行是**主机侧逐行到达间隔**，不是设备节拍。这一项曾经明显大于设备自报的节拍
（当时节拍还是 150 ms）——查下去不是设备的问题，是**上位机自己的读取方式**：

```
pyserial 的 read(size) 会在一个一次性的超时预算里等攒满 size 才返回。
sources.py 原本调 read(4096)，而设备每帧只产出几百字节，
4096 字节要几十帧才凑得满 → 每次 read 都必然等满 timeout 才返回，
把攒下的几行一次性交给状态归约。
```

**这是 `read(4096)` 时期的记录。** 现在节拍 100 ms、`eq_gain_db` 与 `ambient_band_dbfs`
都已下发，一帧 p50 约 **744 B**（`docs/plan-status.md` 第 20 项），凑满 4096 字节所需的
帧数比当时少——但结论不变：
**要与设备自报值比，不要与主机侧到达间隔比。**

当时的症状是 p50 紧贴在读取超时上方、`min ≈ 0.02 ms`（同一批里的第二行）、而设备自报节拍稳定，
序号还会跳变。改成 `read(in_waiting or 1)`（只读现在就有字节，没有就按超时返回）之后，主机侧
到达间隔才与设备自报节拍一致。

当前 100 ms 节拍下实测（2026-10-14，设备在跑、面板 `source=serial`）：设备自报 `capture`
p50 = **100.03 ms**，主机侧到达间隔 p50 = **100.00 ms**。

**口径**：报节拍永远用设备自报的 `capture` 阶段；主机侧那一行只是"上位机有没有跟上"的
自检。两者差得多，先查上位机的读法，不要先怀疑固件。

## 当前证据边界

- 本版可以验证 ESP32-S3 上的环境分析、判据行为、决策稳定性与故障回退
- **不能**证明 CoreS3 扬声器等于 OWS 佩戴听感，也**不能**证明耳机已执行这些参数
- 判据的阈值目前由合成信号标定，**未经真实录音验证**
- 双麦克风可用性、近场信噪比：**均未测量**；声纹可分性已过止损门（D28：WavLM-SV
  间距/抖动 6.41、EER 0%；素材与代理边界见 D27/D28），但 Mac 侧 self/other 门控仍是实验性
- 端侧声纹推理仍未实现；`embed` 阶段报 0 是因为没有实现（见上文三笔账）
- 判据改动可能影响 Mac 侧已排练的行为，标定值（如 `mediaLeakDB`）可能需要重跑
- 上位机显示的是**设备自己报出来的数字**，它不能证明耳机已经执行了这些参数（控制链路仍是 `模拟`）

## 许可

本仓库的代码与文档以 **MIT** 许可发布（见 [`LICENSE`](LICENSE)，Copyright (c) 2026 erantheee）。
`components/bmi270/` 等第三方组件遵循各自目录下的 LICENSE；公开数据集中已跟踪的
`audio.wav` 是维护者确认可公开的素材（保留/撤下策略见
[`experiments/sound-events/data/README.md`](experiments/sound-events/data/README.md)）。

## 本地连接（macOS）

- 串口：`/dev/cu.usbmodem1101`（USB Serial/JTAG，插拔后编号可能变化，可用 `ls /dev/cu.usb*` 确认）
- 看实时日志：`sh tools/monitor.sh`
- 重刷固件：`sh tools/flash.sh`（默认刷 `outputs/current/`，也可传入任意固件目录）
- 端口/工具覆盖：`CORES3_PORT=/dev/cu.usbmodemXXXX`、`ESPTOOL=...`

## 中文字库

界面中文由 `main/lv_font_ui_16.c`（思源黑体子集，210 字形）渲染，不使用 LVGL 内置 CJK 字库。
**新增界面文案前必须先确认用字在字库内**，缺字会显示为空白。完整生成命令记录在该文件头部的
`Opts:` 注释中（生成后需保留该文件的 `#include "lvgl.h"` 与 `.static_bitmap` 两处本地修改）。

当前字库**缺**这些常用字（逐字核对过）：**配对、搜索、设备、信号、进度、曲目、时长、
加载、重试、就绪、返回、文件、插卡**。文案需要绕开它们，或重新生成字库。

已经在字库内、可以放心使用的界面词：`播放` `暂停` `音乐` `连接` `已连接` `链路`
`环境` `增益` `媒体` `控制` `状态` `故障` `麦克风` `正常` `异常`。

（`降噪` 也在字库内，但**设备屏幕现在不该再显示它**——本机不做降噪。把屏幕那一格改成
`调音` 之前，先确认 `调` `音` 两个字在 `main/lv_font_ui_16.c` 里；缺字会显示成空白。）
