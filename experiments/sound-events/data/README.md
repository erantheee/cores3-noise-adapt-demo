# 声音事件 / 蒸馏数据集快照（2026-10-19）

本目录是 CoreS3 声音事件判断与教师-学生蒸馏实验用到的数据。
**原始 16 kHz 音频已包含**（2026-10-19 维护者确认可以公开；音频里有操作者的说话声）。
如果这段音频之后需要撤下，删除对应 `audio.wav` 并更新 `SHA256SUMS` 即可。

## 目录

| 路径 | 内容 |
|---|---|
| `teacher-20261019-multi/` | 一次 183 s 多条件采集的 **16 kHz 音频**（`audio.wav`）、**10 Hz 24 段**（`frames.jsonl`）、分段边界（`phases.json`）、采集元信息（`meta.json`），以及两个教师的逐窗标签（`teacher_yamnet.jsonl`、`teacher_ast.jsonl`） |
| `teacher-20261019-tts/` | 2026-10-19 用 4 个 TTS 音色（Tingting/Meijia/Daniel/Samantha）经 Mac 扬声器播 183 s 的**说话正样本**：16 kHz 音频、10 Hz 24 段、教师 YAMNet 标签 |
| `ambient-20261019-gatefix/` | 12 s 安静环境（无说话）采集：16 kHz 音频 + 10 Hz 24 段 + 设备端 gate 值，用于排查"训练侧电平口径 vs 设备 broadband"不一致 |
| `captures/*.csv` | 规则/学生评估用的逐帧 24 段采集（安静/键盘/风/说话/敲桌），由 `tools/live_band_capture.py` 记录 |
| `SHA256SUMS` | 全部文件的校验和 |

## 采集口径

- 设备：M5Stack CoreS3，板载 ES7210 双麦（当前固件只读单路），产品麦克风。
- 频段：24 段 dBFS，中心频率 70.7 Hz–13.6 kHz（与固件 `controller.cpp` 一致）。
- 节拍：100 ms 控制节拍（无限录音时；有限时长录音会放宽到 250 ms，数据里 `meta.json`
  的 `frame_rate_hz` 会体现，约 9.2–9.4 Hz 是设备实际节拍）。
- 多条件采集用 `tools/collect_teacher_data.py --script`，段顺序：
  `ready 6 s, quiet 20 s, keyboard 35 s, pause 4 s, wind 25 s, pause 4 s, knock 12 s,
  pause 4 s, speech 30 s, pause 4 s, keyboard 20 s, wind 15 s, done 4 s`。
- 音频与频段都带设备 `timestamp_ms`，按设备时间对齐；该采集 0 丢块、0 坏行。

## 隐私与许可

- 原始音频：已包含（维护者确认可公开）。音频含操作者说话声；这是公开仓库，下载即公开。
- 频段/标签：操作者的环境记录；按仓库现有 LICENSE/使用约定处理。
- 教师标签来自公开预训练模型（YAMNet / AST），只是模型输出，不含第三方音频。

## 重新采集（需要设备与上位机）

```sh
# 同时采音频+频段（会独占串口，先停上位机）
python3 tools/collect_teacher_data.py --port /dev/cu.usbmodem101 \
    --out 记录/教师数据/<时间>-multi \
    --script ready:6,quiet:20,keyboard:35,pause:4,wind:25,pause:4,knock:12,pause:4,speech:30,pause:4,keyboard:20,wind:15,done:4
```

教师标签与训练：

```sh
# 官方 YAMNet（隔离 teacher venv，见 experiments/sound-events/README.md）
TFHUB_CACHE_DIR=$PWD/.tfhub-cache experiments/sound-events/.venv-teacher/bin/python \
    experiments/sound-events/teacher_yamnet_official.py <dir>/audio.wav --json <dir>/teacher_yamnet.jsonl

.venv/bin/python experiments/sound-events/train_student.py \
    --train 记录/键盘风声/<...>/live.csv \
    --teacher-session <dir> \
    --holdout 记录/键盘风声/<...>/live.csv
```

## 已知问题

- 数据量小：单次 183 s 采集、两次短采集；跨条件泛化未验证。
- 教师对说话/键盘可靠、对风/敲桌弱（详见 `../README.md` 的"教师实测"）。
- 学生在跨会话留出集上误报未过准入线，因此固件仍用规则版（D18/D19）。
