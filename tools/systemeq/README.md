# tools/systemeq — 第三方播放器 system EQ 原型

QQ 音乐 → Core Audio process tap → `AVAudioUnitEQ`(24 段) → 当前默认输出（耳机）。

S3 仍然只做决策；host 用和 `tools/player` 相同的 stdin JSON 下发 24 段（原型阶段尚未接进 host，可先用 `--bands/--ab/--seconds` 试听）。

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

约束（用户明确）：不同环境场景的过渡保持平滑（沿用 50 ms slew，不加去抖）；**不装限幅器、允许削波**（最终每段夹到 **±24 dB**）；**不允许加延迟**。host 用 `--system-eq-app` 时默认 `--demo` 开。

## stdin 协议（供 host 接入）

```text
{"cmd":"eq","bands":[24 个数],"depth":2.5}
{"cmd":"flat"}
{"cmd":"bypass","on":true}
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
