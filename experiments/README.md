# ESP32-S3 听音情境模型实验分支

本目录属于 `experiment/s3-listening-model` 分支，用来独立验证 ESP32-S3 的端侧模型能力。
它不会改动主项目 `main/` 下已经完成的 CoreS3 Demo 源码。

## 当前模型

- 输入：92 维，沿用主固件已有的 24 频段、电平、语音描述量、运动与时间特征
- 网络：`92 -> 32 -> 16 -> 9`
- 参数：3,657
- float32 权重：14.3 KiB
- 每次推理：3,600 MAC
- 临时激活：32 + 16 + 9 个 float，约 228 B
- 合成验证准确率：95.30%；独立留出：94.44%

这些准确率只验证训练与导出管线，不代表真实场景准确率。

## 2026-10-15 CoreS3 真机结果

- 芯片：ESP32-S3 rev 0.2，实际启动频率 160 MHz
- 固件模型：`v0.2-s3`
- 导出一致性：27/27，`first_mismatch=-1`
- 4,000 次前向：p50 456 us、p95 457 us、max 465 us
- 前后空闲堆：359,820 B，未变化
- 推理约占 100 ms 决策周期的 0.46%
- 三段刷写均返回 `Hash of data verified`

## 与原固件隔离

- `listening-model/`：训练与 C 头文件导出工具。
- `model-bench/`：独立 ESP-IDF 工程，只做 27 条数值自检和推理计时。
- `ear-context/`：声音模型、BMI270 六轴采集、两页屏幕状态与 USB 会话采集整合；已刷入真机，60 秒连续采集零丢帧。活动模型尚未训练。
- 原 Demo 的源码、分区表和 `outputs/current/` 均未修改。
- 原设备完整备份仍在 `outputs/cores3-pre-demo-backup-20261013.bin`。

只编译、不刷机：

```sh
cd experiments/model-bench
sh build.sh
```

未来刷入实验固件后，如需恢复原 Demo，回到仓库根目录使用已经存在的：

```sh
tools/flash.sh
```

恢复前仍应核对串口和 `outputs/current/` 中的 bootloader、partition table、app 三段文件。

