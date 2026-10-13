# CoreS3 Noise Adapt Demo

M5Stack CoreS3 上运行的**耳机盒形态端侧听音算法验证机**。它不是耳机，也不代表智能耳机盒已具备商业价值。

## 当前闭环

```text
CoreS3 双麦克风 → 24 频段环境分析 → 连续策略控制器
                                      ↓
                              EarbudCommand v1
                                      ↓
                         屏幕状态 + USB 串口 JSON
```

CoreS3 作为感知与决策处理器，未来通过 BLE、串口或耳机厂商控制通道把参数交给耳机 DSP。当前没有已接入的可控耳机协议，因此界面如实显示 `控制链路：模拟`。

## 屏幕（中文界面）

正常界面不暴露 Flat、旧 D、New 等工程实验组，只显示产品状态：

- 标题 `端侧算法验证机`，底部 `非最终耳机声学与产品形态`
- `环境`：安静 / 噪声 / 人声嘈杂 / 高噪声
- `当前动作`：正在感知 / 正在调节 / 保持当前设置 / 正在恢复 / 麦克风异常
- `人声增益`：建议的人声增益，硬上限 +6.5 dB
- `降噪`：建议降噪量，硬上限 −8 dB
- `媒体音量`：建议媒体音量比例
- `控制链路`：当前为 `模拟`；真实链路接通并取得回执后才显示 `已发送`
- 底部信息行：麦克风正常/异常、处理耗时与剩余内存

开机不播放任何歌曲，内置扬声器保持静音。麦克风感知与控制器立即开始运行。

## EarbudCommand v1

每约 150 ms 从 USB 串口输出一帧 JSON：

```json
{"type":"earbud_command","schema":1,"sequence":42,"timestamp_ms":6300,"speech_gain_db":3.20,"noise_reduction_db":4.10,"media_percent":84,"state":"ADJUSTING","environment":"VOICE","confidence":0.92,"link":"simulated"}
```

该接口是未来 BLE/耳机 DSP 适配层的边界。没有确认回执前，不得把 `simulated` 改成已连接或已发送。

## 构建

云端（无本地 IDF 时）：推送后由 GitHub Actions 构建，在 Actions 里把产物下载到 `outputs/firmware-<sha>/`。

本地要求 ESP-IDF 5.4.1：

```bash
idf.py set-target esp32s3
idf.py build
```

刷写前必须确认目标是 CoreS3，并保留完整 16 MB 原始固件备份（本机备份位于 `outputs/`）：

```bash
idf.py -p /dev/cu.usbmodemXXX flash monitor
```

## 主机单元测试

```bash
cmake -S test -B /tmp/cores3-noise-adapt-test
cmake --build /tmp/cores3-noise-adapt-test
/tmp/cores3-noise-adapt-test/controller_test
```

## 当前证据边界

这版可以验证ESP32-S3上的环境分析、决策稳定性、故障回退、UI解释和控制协议；不能证明CoreS3扬声器等于OWS佩戴听感，也不能证明耳机已经执行这些参数。真实OWS验证必须接入耳机DSP并取得回执。

## 本地连接（macOS）

- 串口：`/dev/cu.usbmodem1101`（USB Serial/JTAG，插拔后编号可能变化，可用 `ls /dev/cu.usb*` 确认）
- 看实时日志：`sh tools/monitor.sh`
- 重刷固件：`sh tools/flash.sh`（默认刷 `outputs/current/`，也可传入任意固件目录，如 `sh tools/flash.sh outputs/firmware-1c8d363`）
- 端口/工具覆盖：`CORES3_PORT=/dev/cu.usbmodemXXXX`、`ESPTOOL=...`

## 中文字库

界面中文由 `main/lv_font_ui_16.c`（思源黑体子集，211 字形）渲染，不使用 LVGL 内置 CJK 字库（其固定字表缺少“侧验证环益噪链拟发嘈杂调设恢复麦风异终与产态”等界面用字）。新增文案后需要补字重新生成，完整生成命令记录在该文件头部的 `Opts:` 注释中（生成后需保留该文件的 `#include "lvgl.h"` 与 `.static_bitmap` 两处本地修改）。
