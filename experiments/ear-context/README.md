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

## 2026-10-15 真机结果（`e2564c31`，即屏幕无文字的那一版）

采集、推理部分的数据仍然有效；这一版的屏幕是坏的，见下一节。

固件 SHA256 前缀 `e2564c31`，设备 MAC `44:1b:f6:e1:e4:e0`。刷写前做了 16 MB 整片备份，
`outputs/backup-pre-ear-ui-20261015/flash-16mb.bin`，SHA256 `b53e2d56d508399b`。

| 项 | 实测 |
|---|---|
| 六轴采样率 | 100.0 Hz（间隔 p50 10.00 ms、p95 10.11 ms、max 14.97 ms，无 >15 ms 空档） |
| 加速度模长 | 均值 9.781 m/s²（静止，理论 9.807），标准差 0.011 |
| 静止角速度 | 均值 0.26 度/秒 |
| 声音帧间隔 | 100.00 ms（p95 100.01，60 秒 600 帧） |
| 模型推理 | p50 88 µs、p95 91 µs、max 93 µs |
| 60 秒连续采集 | 六轴 6008 条 + 声音 600 帧；`imu_drops`、`context_drops`、`audio_errors` 全为 0 |
| 特征完整性 | 600 帧全部带 92 维特征，无空窗 |

这一轮从真机上抓到并修掉一个静默故障，值得记下来：**六轴一条数据都没有，而且不报错。**
BMI270 的数据就绪位（`INT_STATUS_0` 的 bit7/bit6）只在中断映射打开后才会置位，官方示例里
有 `bmi2_map_data_int(BMI2_DRDY_INT, BMI2_INT1, ...)` 这一步，第一版漏掉了，于是每次轮询都
被判定为"没有新样本"，计数一直停在 0、串口一行 IMU 都没有。现在同时加了 I2C 地址探测
（0x68/0x69 都试）和初始化日志，失败时会打印原因而不是静默变哑。

## 界面

两页，420 ms 刷新一次（每 5 帧）。主页：环境判断、模型分数、声音电平条、六轴状态；
右下按钮切到详情页：声音候选、头部转动速度、模型计算耗时、输出丢弃计数、时钟与保存状态。
分数低于 0.65 显示"暂不确定"，麦克风失败显示"麦克风异常"，六轴无数据显示"未收到数据"。

## 屏幕上一个字都没有：字模格式与 LVGL 配置不匹配（已修）

上一轮把 `e2564c31` 刷进机器后，屏幕上**背景、面板、电平条、按钮都在，但一个字符都没有**。
这不是接线、背光或坐标问题，是字库的存储格式和 LVGL 的配置对不上：

- `main/ear_font16.c` / `main/ear_font24.c` 由 `lv_font_conv` 生成，但漏了 `--no-compress`。
  该工具默认对字模做 RLE 压缩，所以字体描述符里写的是 `.bitmap_format = 1`
  （`LV_FONT_FMT_TXT_COMPRESSED`），字模数据本身也确实是压缩过的。
- 本工程没开 `LV_USE_FONT_COMPRESSED`：Kconfig 里这一项没有 `default y`，
  `build/config/sdkconfig.h` 里也没有 `CONFIG_LV_USE_FONT_COMPRESSED`。
  用本工程真实的编译参数预处理 `lv_font_fmt_txt.c`，可以确认它展开为 `0`。
- 于是 `lv_font_get_bitmap_fmt_txt()` 走 `#else` 分支，对**每一个**字形都 `return NULL`，
  `lv_draw_sw_letter()` 拿到 NULL 就跳过该字形。字形查找本身是成功的（cmap 命中了），
  所以连 `LV_USE_FONT_PLACEHOLDER`（本工程为 y）的占位方块都不会画——文字区域全空。
  该分支的告警走 `LV_LOG_WARN`，而本工程 `LV_USE_LOG = 0`，串口一个字都不打，完全静默。

对照：主工程 `main/lv_font_ui_16.c` 生成时带了 `--no-compress --no-prefilter`，
`.bitmap_format = 0`，在同一颗芯片上显示正常。这是唯一差异。

修法是按同样的符号集重新生成两个字库并加上 `--no-compress --no-prefilter`。
逐个字形核对过：207 + 32 个字形与修复前解压出来的位图**逐像素一致**，只有存储格式变了。
代价是 flash 多 6,143 B，app 分区（0x400000）仍空 82%。

另外在 `ui.cpp` 的 `ui_create()` 里加了开机自检：任一字库的 `bitmap_format` 不是
`LV_FONT_FMT_TXT_PLAIN` 就打一条 `ESP_LOGE`，不再静默变哑。

修后固件 `build/ear_context.bin`，SHA256 前缀 `4763d8b2`，**已刷入** `/dev/cu.usbmodem101`
（`e2564c31` → `4763d8b2`）。三个验证：

- 回读 app 分区（0x10000 起 764,256 B）与本地 `ear_context.bin` 的 SHA256 完全一致，
  确认设备跑的确实是这份镜像，不是残留的旧固件。
- 开机串口没有任何 `E (` 行，也没有新增的自检告警 —— 两个字库都已是 plain。
- 复位后 ES7210 与 BMI270（0x69）正常初始化，六轴 100 Hz、声音 10 Hz，
  `imu_drops`/`context_drops`/`audio_errors` 全为 0，模型推理 ~136 µs。

屏幕的实际观感仍需人眼确认（这一步我替代不了）。
构图预览见 `outputs/ear-ui-fontfix-preview.png`：上排是按故障时的行为渲染的（无文字），
下排是修好后的。这张图是用真机同一份字模数据离线渲染的，不是屏幕截图；
圆角和电平条底色取自 LVGL 默认主题，是近似值。

排查用的两个脚本留在 `tools/`：`font_probe.py` 解析生成的字体 .c 并按 LVGL 的规则解码
单个字形（可用来判断一个字库到底是压缩还是非压缩），`render_ear_ui.py` 按 `ui.cpp` 的
布局渲染整页。

## 编译验证

- ESP-IDF 5.4.1 / ESP32-S3 编译通过。
- 50 组含不规则时间戳的窗口，与 Python 训练管线核对 4600 个特征值通过。
- 两个字库重新生成后，207 + 32 个字形与修复前解压结果逐个逐像素比对一致（0 处差异）。
- 修后整包 `build/ear_context.bin` 764,256 B，`esptool` 报告 app 分区仍空 82%。

## 融合第二轮修掉的两处口径错位（2026-10-16）

`main/window.hpp` 一直在喂模型两个与训练口径不符的输入。两处都不会报错，只会让类别安静地
偏掉——发现它们靠的是把 `features.py`（训练侧权威实现）逐项抄下来对比，不是读代码看出来的：

| # | 原来的做法 | 训练口径 | 实测代价 |
|---|---|---|---|
| 1 | `voice[i] = w[i].frame.voice_likelihood` | `features.py::voice_likelihood()` = 四判据乘积，**没有**时域闸门 | `frame.voice_likelihood` 自 2026-10-16 起乘了 `modulation_term()`（plan-status 31/32 项）。窗里 2 维（`vl_mean`/`vl_std`）吃它 |
| 2 | 时间 5 维硬编码「周一 00:00」= (0, +1, 0, +1, 0) | `when=None` 默认 = (0, −1, 0.974928, −0.222521, 0) | 归一化后差 **4.04σ**；拿 model-bench 的 27 条自检向量实测，**3 条改了 argmax** |

两处都已改成与 `main/context_window.hpp` 完全一致的算法（第 1 条改为
`voice_likelihood_from_features(w[i].vf)`，`vf` 本来就逐帧存着）。**`main/context_window.hpp`
是权威版本，改这里之前先看那边。**

顺带说明第 2 条的背景：训练时每条 clip 都抽了随机时间（`make_dataset.py::random_when`），
所以这 5 维在训练集里**是有分布的**（σ = 0.28–0.77），不是常量；渲染时的"固定基准"必须
落在那个分布里，而不是随便挑一个看起来干净的时刻。

## 仍未做的

- 佩戴位置与轴向标定：当前数据是桌面平放采集的，耳戴时的轴向需重新记录
- 活动分类模型：`activity` 字段恒为 `untrained`
- 设备时钟：`clock_valid=false`，时间特征用固定基准
- 长时漂移：只跑了 60 秒，一小时级别的漂移还没测

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

## 自我语音可分性探针（轴2，未采）

背景：产品要判"我是否在说话"。现在这件事**只有一条路 —— 声纹 embedding**（主工程
`voiceprint_gate`，以及 OWS 项目里的 `SelfVoiceStore`）。而声纹那条路带一个还没跑的
止损门（`docs/plan-status.md` 第 12 项：间距/抖动 ≥ 3 且 EER ≤ 5% 才继续）。

而且它有一个已经实测到的失效模式，记在 OWS 项目里：**没注册声纹时 `selfVoiceMatched`
恒为 false，系统把本人当外部人声 —— 你自己说话也会把音乐压下去。** 单点。

六轴能不能提供一条**独立于声学**的自我语音线索（说话时下颌带动头部微动），就是这两个
脚本要回答的问题。**只回答这一个问题**，不训练模型、不导出权重、不碰固件。

```sh
# 1. 采：六轴 100 Hz + Mac 麦克风，按脚本走分段，起止各拍一下桌子做同步脉冲
python3 collect_selfvoice.py --port /dev/cu.usbmodem101 --subject p01 --position right-ear

# 2. 出一份可分性报告
python3 selfvoice_report.py --session ../listening-model/data/real/<会话目录>
```

先决条件按重要性排：**设备必须戴在耳朵上**（桌面平放的数据对这个问题没有意义）、
终端要有麦克风权限、音频要能被 `ffmpeg` 录到（`--no-audio` 可以跳过，但就失去了独立的
语音真值）。

协议里后面三段是**负样本**，比正样本更重要：`fidget`（抖腿）、`chew_nod`（咀嚼/点头）
都有明显的头部运动，如果分不开"我在说话"和"我在咀嚼"，这条轴就没有价值。

评估用**留一段法**并把各折预测汇总后算一个 AUC。逐折算 AUC 是错的：留出的那一段本身
只有一个标签，AUC 没有定义（第一版就踩了这个坑）。同一段的窗口高度相关，随机拆窗口会
把分数抬高，所以必须整段留出。

两个脚本都用合成数据做过对照：植入语音带信号时报告给"有信号但不够干净"，不植入时给
0.472（≈随机）并判"基本不可分"。**报告不会在没信号的时候说成功。** 用真机数据跑过
12 秒短流程（`--quick`），六轴实测 100.2 Hz，麦克风录到 48 kHz 单声道。

判据先定：**汇总 AUC ≥ 0.85 才继续做模型；0.70–0.85 先看混淆在哪；< 0.70 直接停。**

## 驱动来源

https://github.com/boschsensortec/BMI270_SensorAPI
提交 `41129fcfe39c583ee5462d79195741945d51c1fe`，BSD-3-Clause。
仅复制 bmi2/bmi270 所需源文件与许可证，不创建嵌套仓库。
驱动复用不代表活动分类模型复用。
