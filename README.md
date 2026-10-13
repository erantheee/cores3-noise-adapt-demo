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

CoreS3 作为感知与决策处理器，未来通过 BLE、串口或耳机厂商控制通道把参数交给耳机 DSP。当前没有已接入的可控耳机协议，因此界面如实显示 `EARBUD LINK: SIMULATED`。

## 屏幕

正常界面不暴露 Flat、旧 D、New 等工程实验组，只显示产品状态：

- `ENV`：环境状态与麦克风 dBFS
- `ACTION`：观察、调整、保持、恢复或麦克风故障
- `VOICE GAIN`：建议的人声增益，硬上限 +6.5 dB
- `NOISE RED.`：建议降噪量，硬上限 −8 dB
- `MEDIA`：建议媒体音量比例
- `EARBUD LINK`：当前为模拟；真实传输接通后显示已发送

开机不播放任何歌曲，内置扬声器保持静音。麦克风感知与控制器立即开始运行。

## EarbudCommand v1

每约 150 ms 从 USB 串口输出一帧 JSON：

```json
{"type":"earbud_command","schema":1,"sequence":42,"timestamp_ms":6300,"speech_gain_db":3.20,"noise_reduction_db":4.10,"media_percent":84,"state":"ADJUSTING","environment":"VOICE","confidence":0.92,"link":"simulated"}
```

该接口是未来 BLE/耳机 DSP 适配层的边界。没有确认回执前，不得把 `simulated` 改成已连接或已发送。

## 构建

要求 ESP-IDF 5.4.1：

```bash
idf.py set-target esp32s3
idf.py build
```

刷写前必须确认目标是 CoreS3，并保留完整 16 MB 原始固件备份：

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
