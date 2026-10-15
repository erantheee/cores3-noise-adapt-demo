# 耳戴环境感知整合实验

属于原仓库的 `experiment/s3-listening-model` 分支。根目录原应用保持不变。
独立构建目标 `ear_context`，编译不刷机。刷入后会替换设备当前运行程序。

## 已接入的代码

- CoreS3 ES7210 单声道 48 kHz 输入，24频段分析，每4800样本更新一次。
- 最近20帧输入现有 v0.2-s3 环境声音模型，复用原模型权重。
- Bosch BMI270 官方驱动，100Hz，加速度 ±4g、角速度 ±500°/s。
- IMU 与声音使用同一个 ESP 单调时钟，JSON 记录读完成时间；不是严格硬件同步。
- 屏幕展示环境模型输出、未校准的 softmax 分数、麦克风电平、IMU状态与推理耗时。
- USB 输出六轴数据、频谱、92维特征、丢队列计数及读取错误。采集脚本按会话落盘。

## 明确边界

代码集成与编译通过不等于真机采集验证。尚需刷入后检查轴向、重力模长、采样率、
音频吞吐、长时丢帧、屏幕和耳戴固定方式。
声音模型仍以合成数据训练，分数不是已校准的真实正确率。
IMU仅采集，活动状态为 untrained，尚未导入 Auritus 权重或训练耳戴分类器。
模型运动输入仍为0，不能把新六轴数据直接塞入旧模型。设备时钟未同步，时间特征使用
固定 Monday 00:00 基线，记录 clock_valid=false。没有真实时间上下文功能。
耳机未连接，未输出或执行听音调节，也不播放声音。
IMU轮询不是FIFO无损采集：USB或任务拥塞可能漏读，队列溢出另计。训练前必须检查间隔。
USB输出通过独立低优先级任务处理，采集任务不等待电脑读取；分别记录IMU和声音队列丢弃计数。

## 本轮验证

- ESP-IDF 5.4.1 / ESP32-S3 编译通过，应用 753392 字节。
- 50组含不规则时间戳的窗口，与Python训练管线核对4600个特征值通过。
- 未刷入、未产生真实采集会话，100Hz是配置值而非已验证吞吐。

## 使用

```sh
sh build.sh
python3 collect.py --port /dev/cu.usbmodem101 --seconds 60 \
  --subject p01 --position left-ear --label sitting
```

采集需设备先运行本固件，Python安装 pyserial。`--seconds 0` 持续到 Ctrl-C。
默认保存至原仓库 `experiments/listening-model/data/real/<UTC会话时间>/`，已被Git忽略。
包含 session.json、samples.jsonl、summary.json。不会保存原始录音；不会在没有采集程序时
持久化到设备。human_activity_label 必须由人提供，模型预测不得当作训练真值。
新格式不使用旧 collect_from_device.py 的频段近似转换；92维特征已随每次有效推理导出。
数据按人/会话划分训练与测试，不能随机拆分相邻重叠窗口。

## 驱动来源

https://github.com/boschsensortec/BMI270_SensorAPI
提交 `41129fcfe39c583ee5462d79195741945d51c1fe`，BSD-3-Clause。
仅复制 bmi2/bmi270 所需源文件与许可证，不创建嵌套仓库。
驱动复用不代表活动分类模型复用。
