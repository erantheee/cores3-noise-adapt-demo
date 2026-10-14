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
CoreS3 双麦克风 → 24频段环境分析 → 人声/噪声/对话形态 → 端侧策略
                                                           ↓
                                                  EarbudCommand v2
                                                           ↓ USB
                                        Mac三首本地歌曲处理 → AirPods Pro
```

CoreS3 承担耳机仓侧的感知、判断与决策；Mac 承担本次 Demo 的音频执行；AirPods Pro 只播放 Mac
最终输出。AirPods 不开放双耳原始麦克风与通透 DSP，所以当前不能声称使用 AirPods 麦克风完成了
目标人声增强。真实执行链路尚未接通时，协议固定上报 `execution: decision_only`。

## 三笔账的当前状态

**账 1 分段延迟：端侧部分已实测。** 固件 `be4cba7`，空载运行：

| 阶段 | p50 | 说明 |
|---|---|---|
| 采集间隔 | **150.032 ms** | 就是控制节拍，目标 150 ms |
| 24 频段分析 | **7.032 ms** | 占节拍的 4.7% |
| 声纹推理 | 0.000 ms | **未实现**，如实报 0 |
| 决策 | **0.051 ms** | 50 微秒 |
| 打点自身开销 | 1.41 µs | 两次读时钟 |

**端侧本机不是瓶颈：分析与决策合计 7.1 ms，节拍余量 95%。** 剩下的是音频通路本身
（蓝牙 311 ms），那一段与端侧算力无关。

**但不要说成"端侧 AI 很快"**：声纹推理还没实现，它是这套流程里唯一真正的神经网络负载，
报 0 是因为没有，不是因为免费。

| # | 待回答 | 状态 | 卡在哪 |
|---|---|---|---|
| 1 | 一小时连续运行的延迟漂移 | 未测 | 需设备在场跑 `tools/drift_capture.py` |
| 2 | 近场拾音信噪比 vs Mac 的 **0.9 dB** | 未测 | 需录三类音频；双麦是否可用也待验 |
| 3 | 判据阈值用真实录音标定 | 未做 | 需录安静本底/白噪声/纯音/人声 |
| 4 | 声纹可分性（类间间距 vs 类内抖动） | 未测 | 需同一人**分次、换距离换音量**录多条 |

第 4 项带**止损门**：间距/抖动 ≥ 3 且 EER ≤ 5% 才继续，否则停止声纹路线。工具
`tools/voice_separability.py` 已就位且判据写死。理由见 [`docs/decisions.md` D7](docs/decisions.md)。

## 真机踩到的坑（换硬件前请先读）

这三条都**只在真机上暴露**，桌面机上完全看不见，写在这里是为了不要重犯：

1. **桌面基准会骗人。** 把 24 频段分析里的 Hann 窗提出循环外，真机上 57.1 ms → 7.0 ms
   （8 倍），桌面上只有 14%——因为桌面编译器会向量化那段循环，ESP32-S3 不会。
2. **控制节拍超时的真因不是日志。** 周期 170 ms 对 150 ms 目标，第一反应是串口阻塞，
   于是把波特率从 115200 提到 921600：**周期一点没变，假设被推翻**。真因是读取粒度——
   节拍判断只能在一次读返回后执行，永远跨过截止点，平均超出半个读取块（2048 样本 = 21 ms）。
   降到 512 样本后精确命中 150 ms。
3. **"打点开销"很容易测成"被测对象"。** 这条度量我写错了三次，前两版分别报 57 ms 和
   7.1 ms，**都是把分析本身算了进去**，因为结束时刻的读时钟放在了工作之后而不是紧挨着开始。
   现在相邻两次读时钟，1.41 µs。

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

开机不播放任何歌曲，内置扬声器保持静音。

## EarbudCommand v2

每约 150 ms 从 USB 串口输出一帧 JSON：

```json
{"type":"earbud_command","schema":2,"sequence":42,"timestamp_ms":6300,"external_voice_gain_db":3.20,"media_percent":100,"state":"ADJUSTING","environment":"VOICE","mode":"EXTERNAL_VOICE_INTENT","conversation":"LIKELY","confidence":0.92,"execution":"decision_only","link":"simulated","eq_gain_db":[0.0,...,0.0],"ambient_band_dbfs":[-52.1,...,-61.0]}
```

没有 Mac 执行回执前，`execution` 必须保持 `decision_only`。现实人声模式下 `eq_gain_db` 必须平直；
否则就是把现实人声增强错误替换成歌曲中频增强。

`eq_gain_db` 是 S3 给 Mac 的歌曲 EQ 建议，`ambient_band_dbfs` 是 S3 实测环境谱；Mac 还要结合
EQ 前的歌曲内容生成最终处方。`external_voice_gain_db` 是现实人声增益意图，不代表 AirPods 已执行。

`noise_reduction_db` 是**降噪量的大小**，取值 0…+8，不是负值偏移（固件的钳位是
`std::clamp(noise_reduction_db_, 0.0f, kMaxNoiseReductionDb)`，`main/controller.cpp`）。

**本机不做降噪**（2026-10-14 用户明确口径）：动作只有**增益与调音**。这个字段曾是固件里的
残留——控制器算了它、设备屏幕把它列成「降噪」、JSON 也照发，而它在整条链路里没有任何
消费者。**2026-10-14 已从固件删除**：`EarbudCommand` 里没有这个字段了，设备屏幕上那一格
改成「调音」，JSON 也不再发它。上位机的解析层仍接受它，只为读得懂升级前的历史日志。

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

当前 6 个套件、**181 条断言**：

```bash
cmake -S test -B build-host-tests
cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
```

CI 里这一步跑在固件构建**之前**（[`.github/workflows/build.yml`](.github/workflows/build.yml)），
单测不过不会出固件。

上位机（`tools/host/`）另有一套自己的自测，同样是纯 Python、秒级、不需要设备：

```bash
python3 tools/host/tests/test_host.py            # 33 组、1055 条断言
node tools/host/tests/render_check.js <快照.json> # 可选：用 DOM 桩真跑一遍界面渲染
```

`render_check.js` 存在的原因很具体：这台机器上无头浏览器拿不到显示器，系统也不允许截屏，
所以"打开看一眼"这条路是断的。它把 DOM 换成桩、喂进一份真实快照
（`curl -s http://127.0.0.1:8787/api/snapshot > /tmp/s.json`），断言 render() 到底写了什么。

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
# 采集（需要带 pyserial 的解释器，macOS 自带 python3 通常没有）
/Users/eranthe/Documents/Codex/2026-10-10/du/work/uv-cache/archive-v0/LSKE5uaT0ygsXUNE/bin/python \
    tools/drift_capture.py --port /dev/cu.usbmodem1101 --seconds 3600 --out 记录/drift.jsonl

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

一个本地面板：把设备每 150 ms 输出的那一帧变成看得见的数字，并可以回控设备录音。
**它是本仓库自带的**，不改动你已排练的 `ows-earbud-demo` Swift 工程。

```bash
sh tools/host_up.sh                    # 真机（自动找 /dev/cu.usbmodem*）
sh tools/host_up.sh --mock             # 没有设备也能先把界面跑起来
sh tools/host_up.sh --replay 记录/x.log --speed 4    # 回放历史日志
# 然后打开 http://127.0.0.1:8787/
```

面板会显示：环境 / 当前动作 / **语音段增益** / **调音（24 频段 EQ 形状）** /
建议媒体音量 / 控制链路 / 置信度、最近 90 秒的趋势、端侧分段延迟（每 10 秒一次汇总）、
状态变化与异常列表、以及最近几行设备原始输出。

**面板上没有"降噪"**：本机的动作只有增益与调音，所以那一格、图例里的那条曲线都删掉了。

**四条口径写死在界面上，不靠人记：**

| 界面上的东西 | 口径 |
|---|---|
| 控制链路 | 设备报 `simulated` 就显示 `模拟`；没有确认回执前不得写成已连接或已发送 |
| `n = 0` 的延迟阶段 | 显示「未实现」。声纹推理报 0 是因为没实现，**不是因为它免费** |
| 序号跳变 | 趋势图上画一条红色竖线，异常列表里写明丢了几帧；曲线不会替设备"连过去" |
| 数据年龄 | 端口开着但没帧时显示「已连接（无数据）」并告警——节点在 ≠ 设备在 |

**回控是白名单，不是串口终端。** 界面只能发 `rec start [秒数]` 与 `rec stop`
（设备侧只实现了这两条，见 `main/app_main.cpp`）。别的输入一律拒绝，不做"顺手转发"。
在 `--mock` / `--replay` 下没有设备可写，回控会明确返回拒绝。

### ⚠️ 这台设备上录音按钮目前**用不了**，而且上位机会如实这么说

实测（2026-10-14）：命令能写进串口（`write()` 成功、macOS 侧 `out_waiting = 0`、tty 标志
正常），但**设备没有任何反应**——不会出现 `{"type":"capture","state":"started"}`，
`mic_chunk` 一个都不来。所以按钮现在返回的是这样一条：

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

**修法**（2026-10-14 已落地到源码）：把控制台 I/O 从 UART0 改为内置 USB-Serial/JTAG，
让 **stdin 和 stdout 走同一条链路**：

```
sdkconfig.defaults:
  CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y      # 主控制台 = 内置 USB-Serial/JTAG
  CONFIG_ESP_CONSOLE_SECONDARY_UART0=y      # UART0 留作次要控制台，接串口适配器仍能看日志
```

**代价**：设备用充电器供电（不接电脑）时就没有控制台输出了。对这台台架设备可以接受——
另一个选择是它永远收不到命令。

**面板里的录音块不会自动落盘。** 落盘仍然由 `tools/record_from_device.py` 负责，
因为它同时报告丢块；两套写盘逻辑各记一半比不记更糟。

### 端口被占用时它会自己往前走

默认 8787。被占用时**不会退出**，而是向后找端口并如实打印实际地址——现场最常见的占用
原因是上一次没退干净的上位机（本次实测就是：`ows-earbud-demo` 的 `demo` 还在跑，占着
8787，所以上位机落在 8788）。

### 上位机自测

```bash
python3 tools/host/tests/test_host.py      # 33 组、1055 条断言，不需要设备、不需要串口
```

它自带一份真机日志 `tools/host/tests/fixtures/device_real.log`（2026-10-14 抓的**节选**，
41 帧连续原文，对应设备运行约 17.6–23.6 s；不是整段会话）。
解析器以后被改动时，必须仍然读得懂这台设备真正吐出来的字节，而不是只读得懂测试里手写的 JSON。

### 一个必须分清的口径：主机侧帧到达间隔 ≠ 设备节拍

面板「统计」里那三行是**主机侧逐行到达间隔**，不是设备节拍。这一项曾经报出 p50 = 202 ms，
而设备自报采集间隔是 150.04 ms——查下去不是设备的问题，是**上位机自己的读取方式**：

```
pyserial 的 read(size) 会在一个一次性的超时预算里等攒满 size 才返回。
sources.py 原本调 read(4096)，而设备每 150 ms 只产出约 180 字节，
4096 字节要 23 帧才凑得满 → 每次 read 都必然等满 timeout=0.2 s 才返回，
把攒下的 1–2 行一次性交给状态归约。
```

表现就是：p50 紧贴在 200 ms 上方（实测 202.3）、`min ≈ 0.02 ms`（同一批里的第二行）、
而设备自报节拍始终是 150.04 ms、序号跳变为 0。**面板因此最多滞后约 208 ms。**

改成 `read(in_waiting or 1)`（只读现在就有字节，没有就按超时返回）之后实测：

| | p50 | min | max |
|---|---|---|---|
| 修复前 | 202.3 ms | 0.02 ms | 205.3 ms |
| **修复后** | **150.0 ms** | 149.71 ms | 150.4 ms |

**口径**：报节拍永远用设备自报的 `capture` 阶段；主机侧那一行只是"上位机有没有跟上"的
自检。两者差得多，先查上位机的读法，不要先怀疑固件。

## 当前证据边界

- 本版可以验证 ESP32-S3 上的环境分析、判据行为、决策稳定性与故障回退
- **不能**证明 CoreS3 扬声器等于 OWS 佩戴听感，也**不能**证明耳机已执行这些参数
- 判据的阈值目前由合成信号标定，**未经真实录音验证**
- 双麦克风可用性、近场信噪比、声纹可分性：**均未测量**（分段延迟已实测，见上文表格）
- 判据改动可能影响 Mac 侧已排练的行为，标定值（如 `mediaLeakDB`）可能需要重跑
- 上位机显示的是**设备自己报出来的数字**，它不能证明耳机已经执行了这些参数（控制链路仍是 `模拟`）

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
