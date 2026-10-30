# tools/systemeq — 第三方播放器 system EQ 原型

QQ 音乐 → Core Audio process tap → 24 段轻量 DSP → 当前默认输出（耳机）。

S3 只做环境判断；host 把它映射成幅度受限的环境处方。Mac 同时用 1024 点 FFT
检测高频密度与瞬态风险，对鼓点、钢琴起音等强瞬态收紧正增益。实时主链
**不运行分轨模型**、不等 2 s 窗、不增加算法延迟。

## 构建 / 运行

```bash
sh tools/systemeq/build.sh

# 在线 QQ 音乐播放中，试听 12 s，每 3 s 在 EQ / 直通之间切换：
./tools/systemeq/systemeq \
  --app com.tencent.QQMusicMac \
  --bands "-1.6,-1.6,-1.6,-0.8,-0.8,-0.8,-0.8,-0.8,2.4,2.4,2.4,2.4,2.4,2.4,2.4,2.4,2.4,2.4,1.2,1.2,1.2,1.2,1.2,1.2" \
  --depth 2.5 --ab 3 --seconds 12
```

参数：`--app`（bundle id）、`--bands`（24 个逗号分隔）、`--depth`（默认 2.5）、`--demo`（叠加固定 demo 基线）、`--ab`（每 N 秒切换 EQ/直通）、`--seconds`（0=一直跑）、`--report`。

### demo 基线（2026-10-22 用户口径）

`--demo` 开启后在 S3 自适应 24 段之上叠加一条**固定基线**（dB，不乘深度）：

```text
70/141 Hz +6 · 245 Hz 0（糊区高危，不抬）· 346–842 Hz −1…−3（去糊）
1–4 kHz +2…+6（presence 驼峰）· 7–13.6 kHz +4…+6（空气）
```

版本史：第一版全翻倍 → 用户嫌"太爆炸"减半 → 2026-10-22 晚用户重新拍板
**"demo 要明显"**，整体 ×2 恢复翻倍版（245 Hz 保持 0，与固件
`kSongEqContentDb` 的网友共识校准一致）。

默认 EQ 路径的不同环境场景保持平滑（沿用 50 ms slew），**不增加算法延迟**。
2026-10-29 加了 EQ 链内 `globalGain` 作为输出余量：直接测 EQ 后峰值，超过 0.94
就快速收紧，低于后缓慢恢复。它是零延迟的后级余量，不是添加预读的限幅器。
host 用 `--system-eq-app` 时仍会开基础基线；现场剂量已收小，不再使用 S3 旧的 24 段形状。

另有仅供离线实验的 stem 路径：`--stem-worker`。它用 2 s 滚动窗做六轨估算，固定延迟约
2.8 s。2026-10-29 真机曾测到 **2.736 s/窗、RTF 1.368**，输出缓冲读空、输入 ring 塞满，
听感为严重断续。现在它不是默认路径；且一旦 ring 持续积压，就 fail-open 且本次 host
不再自动重启分轨。

## stdin 协议（供 host 接入）

```text
{"cmd":"eq","bands":[24 个数],"depth":2.5}
{"cmd":"flat"}
{"cmd":"bypass","on":true}
{"cmd":"stem_environment","value":"quiet|broadband|keyboard|wind|babble"}
{"cmd":"status"}
{"cmd":"quit"}
```

stdout：`{"event":"ready",...}` / `{"event":"state",...}`（含 `rms/peak/ring_fill/consumed`）/ `{"event":"ab",...}` / `{"event":"error",...}`。stdin EOF 不退出（方便 `--seconds` 试听）。

## 关键坑（已实测，2026-10-22 在线曲目）

1. **聚合设备用空 subdevices + master=0**，并且**建 IOProc 之前先读一次 `kAudioAggregateDevicePropertyComposition`**，否则 `AudioDeviceStart` 返回 `1852797029 ('nope')`。
2. **不要把默认输出 UID 塞进 `kAudioAggregateDeviceMasterSubDeviceKey`**：会让 IOProc 跑到约 1.5×（producer 72k+/s），ring 溢出。`SYSTEMEQ_MASTER=output` 可复现此坑。
3. `muteBehavior` 用 `mutedWhenTapped`（默认）；`SYSTEMEQ_MUTE=0/1/2` 可覆盖调试。
4. QQ 音乐必须正在出声，否则 tap 聚合设备起不来。
5. 退出/失败要销毁 tap 让原声恢复（fail-open）。stdin EOF 不退出。

## 验证记录

- 在线曲目，`PROBE_MUTE=0` 只读探测：48 kHz / 2 ch，3 s、284 回调、290,816 floats、RMS 0.308、peak 0.988。
- systemeq 12 s A/B：`start=0`；producer/consumer 均约 48 kHz；`ring_fill` 稳定在 3–4k（不溢出）；`bypass` 每 3 s 切换，EQ 曲线（低切 -4/-2 dB、1–4 kHz +6 dB、高 +3 dB）生效。
- 2026-10-29 低延迟主链实时验收：QQ 音乐输出源峰值 0.977，EQ 后峰值
  0.974–0.976，`out_clip_frames=0`；`ring_fill` 在 25–26k 稳定，无分轨进程。
- 2026-10-29 S3 24 段掩蔽矩阵：内容端复用 1024 点 FFT 生成 24 段相对谱形，与
  `ambient_band_dbfs` 做归一化重叠判断；低/中/高段补偿上限分别为 2.35/2.50/2.35 dB。
  可听 demo 档在强重叠证据成立时反推瞬态门前目标，使低/中/高任一被遮蔽段在安全门后约为
  2.15 dB（为平滑后实际值留出至少 2 dB 的余量）；真正的强瞬态允许临时收缩。仍经内容瞬态门与
  输出余量，后级峰值目标约 -0.9 dBFS。真机已见 `masking_matrix_enabled=true`、24+24 段齐全、
  非分轨 `base_gain=0`；24 s 观察 ring 稳定，启动初期 1 个削波采样后未继续增长。
- **2026-10-30 真机验收（60 s / 563 帧，`记录/eq验收/20261030-live-1.csv`）**：
  `masking_peak_db` P50 2.22 dB；`applied_bands` 正向峰值 P50 **2.14** / P90 2.22 /
  max 2.29 dB，≥2 dB 占 85%，P50 17/24 段参与；窗口内 `out_clip_frames` 增量 0
  （启动期累计 13 帧，源峰值 1.0033 已越过全量程）；输出余量中位 −2.93 dB；丢帧 0。
  注意 `applied_bands` 是全局余量前的相对值，听感是"谱形变了"而不是"更响"。
