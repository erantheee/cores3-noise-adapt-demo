# CoreS3 端侧可行性验证机

M5Stack CoreS3 上运行的**耳机盒形态端侧 AI 可行性验证机**。它不是耳机，也不代表智能耳机盒
已具备商业价值。

**它要回答的不是"算法好不好"**——那件事已经由
[`ows-earbud-demo`](https://github.com/erantheee/ows-earbud-demo) 在 Mac 上完成并留下实测数据。
它回答的是另外三笔账：**端侧的分段延迟、端侧的近场拾音信噪比、声纹选人在端侧是否可行。**

完整论证见 [`docs/edge-feasibility.md`](docs/edge-feasibility.md)；每条决定的原因见
[`docs/decisions.md`](docs/decisions.md)；**计划本身与每项状态见
[`docs/plan-status.md`](docs/plan-status.md)**。改行为之前先读这三份。

---

## 当前闭环

```text
CoreS3 双麦克风 → 24 频段环境分析 → 人声/噪声判据 → 连续策略控制器
                                                        ↓
                                                 EarbudCommand v1
                                                        ↓
                                            屏幕状态 + USB 串口 JSON
```

CoreS3 承担感知、判断与决策。**音频链路不由它负责**——真实产品里耳机盒也不参与音频链路，
音频由播放端经蓝牙送达耳机。当前没有已接入的可控耳机协议，因此界面如实显示
`控制链路：模拟`。

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

- 标题 `端侧算法验证机`，底部 `非最终耳机声学与产品形态`
- `环境`：安静 / 噪声 / 人声嘈杂 / 高噪声
- `当前动作`：正在感知 / 正在调节 / 保持当前设置 / 正在恢复 / 麦克风异常
- `人声增益`：建议的人声增益，硬上限 +6.5 dB
- `降噪`：建议降噪量，硬上限 −8 dB
- `媒体音量`：建议媒体音量比例
- `控制链路`：当前为 `模拟`；真实链路接通并取得回执后才显示 `已发送`
- 底部信息行：麦克风正常/异常、处理耗时与剩余内存

开机不播放任何歌曲，内置扬声器保持静音。

## EarbudCommand v1

每约 150 ms 从 USB 串口输出一帧 JSON：

```json
{"type":"earbud_command","schema":1,"sequence":42,"timestamp_ms":6300,"speech_gain_db":3.20,"noise_reduction_db":4.10,"media_percent":84,"state":"ADJUSTING","environment":"VOICE","confidence":0.92,"link":"simulated"}
```

没有确认回执前，不得把 `simulated` 改成已连接或已发送。

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

当前 6 个套件、**178 条断言**：

```bash
cmake -S test -B build-host-tests
cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
```

CI 里这一步跑在固件构建**之前**（[`.github/workflows/build.yml`](.github/workflows/build.yml)），
单测不过不会出固件。

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
python3 tools/monitor.sh          # 或直接用下面的录音脚本

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

## 当前证据边界

- 本版可以验证 ESP32-S3 上的环境分析、判据行为、决策稳定性与故障回退
- **不能**证明 CoreS3 扬声器等于 OWS 佩戴听感，也**不能**证明耳机已执行这些参数
- 判据的阈值目前由合成信号标定，**未经真实录音验证**
- 双麦克风可用性、近场信噪比、声纹可分性、分段延迟：**均未测量**
- 判据改动可能影响 Mac 侧已排练的行为，标定值（如 `mediaLeakDB`）可能需要重跑

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
`环境` `增益` `降噪` `媒体` `控制` `状态` `故障` `麦克风` `正常` `异常`。
