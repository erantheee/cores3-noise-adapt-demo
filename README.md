# CoreS3 端侧可行性验证机

M5Stack CoreS3 上运行的**耳机盒形态端侧 AI 可行性验证机**。它不是耳机，也不代表智能耳机盒
已具备商业价值。

**它要回答的不是"算法好不好"**——那件事已经由
[`ows-earbud-demo`](https://github.com/erantheee/ows-earbud-demo) 在 Mac 上完成并留下实测数据。
它回答的是另外三笔账：**端侧的分段延迟、端侧的近场拾音信噪比、声纹选人在端侧是否可行。**

完整论证见 [`docs/edge-feasibility.md`](docs/edge-feasibility.md)；每条决定的原因见
[`docs/decisions.md`](docs/decisions.md)。**改行为之前先读这两份。**

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

## 三笔待量的账

| # | 待回答 | 当前状态 |
|---|---|---|
| 1 | 分段延迟（采集/帧内计算/推理/决策/传输/生效/出声） | **未测量**。这是老板焦虑的直接答案 |
| 2 | 近场拾音信噪比 vs Mac 实测的 **0.9 dB** | **未测量**。双麦是否可用也尚未验证 |
| 3 | 声纹可分性（类间间距 vs 类内抖动） | **未测量**。工具已就位：`tools/voice_separability.py` |

第 3 项带**止损门**：间距/抖动 ≥ 3 且 EER ≤ 5% 才继续，否则停止声纹路线。理由见
[`docs/decisions.md` D7](docs/decisions.md)。

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

```bash
cmake -S test -B build-host-tests
cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
```

没有 cmake 时可以直接编译（等价）：

```bash
c++ -std=c++17 -I main test/controller_test.cpp main/controller.cpp -o /tmp/controller_test && /tmp/controller_test
c++ -std=c++17 -I main test/noise_analyzer_test.cpp main/controller.cpp -o /tmp/noise_test && /tmp/noise_test
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
