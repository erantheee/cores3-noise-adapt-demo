# 端侧听音情境模型 v0.1 —— 训练管线

给 CoreS3（耳机盒）训练一个本地小模型：用固件**已经在算**的 24 频段环境特征 + 时间，
判断"当前是什么情境"，为"怎样调、何时调、何时退出或不动作"提供决策输入。

设计前提（决定了所有取舍）：

* **不引入新的 DSP**。设备每 100 ms 已经在算 24 个频段的 Goertzel 与语音四判据，模型只消费
  这些量 + 时间 + IMU 运动量。端侧新增的计算只有一次 92→128→64→9 的前向（20,745 参数）。
* **训练口径与固件口径逐位对齐**。帧长 2560 样本（固件 kFrameSamplesMax）、48 kHz、
  100 ms 决策节拍、最近 20 帧（2 秒）为一个样本。口径错位不会报错，只会让设备上的表现变差，
  所以有专门的跨实现校验脚本（见下）。
* **合成语料只用于把管线跑通**。真实场景准确率必须用真实录音验证；合成语料的已知短板
  （高频本底不足）在实测中已经暴露。

## 目录

| 路径 | 作用 |
|---|---|
| `scripts/features.py` | 与固件对齐的特征提取（Goertzel、语音判据、窗特征 92 维） |
| `scripts/selftest_features.py` | 复刻自检：必须复现固件实测的判据行为 |
| `scripts/synth.py` | 合成 9 类情境语料（程序化噪声 + 真实 TTS 音色 + 真实音乐） |
| `scripts/make_dataset.py` | WAV -> 特征数据集（按整段音频划分训练/验证） |
| `scripts/train.py` | 训练 MLP（PyTorch），输出 .pt + 报告 JSON |
| `scripts/export_c.py` | 导出 C 头文件（归一化折进第一层）+ 自检向量 + 导出回验 |
| `scripts/host_check.cpp` | 在主机上编译导出头文件，验证 C 代码路径 |
| `scripts/collect_from_device.py` | 从 CoreS3 采集带标签的真实环境数据并转数据集 |
| `mac_demo/` | Mac 上先跑：固件同款分析器 + 同一份权重（实时/文件两种模式） |
| `artifacts/` | 数据集、模型、训练报告 |

设备侧基准固件在 `../model-bench/`（独立 ESP-IDF 工程，含导出的 listening_model.h 与
test_vectors.h），编译与刷写：

```sh
cd ../model-bench
sh build.sh          # 只编译
sh build.sh flash    # 仅在明确决定刷实验固件后使用
```

## 完整流程（可复现）

```sh
cd work/listening-model

# 0) 环境（一次性）
UV_CACHE_DIR=$PWD/.uv-cache uv venv --python /Users/eranthe/.local/bin/python3.12 .venv
UV_CACHE_DIR=$PWD/.uv-cache uv pip install --python .venv/bin/python torch numpy scipy

# 1) 复刻自检：Python 必须复现固件的判据行为
python3 scripts/selftest_features.py

# 2) 合成语料（TTS 池在 data/tts/，见 scripts/synth.py 头部说明）
python3 scripts/synth.py --clips 60 --seconds 8
python3 scripts/synth.py --clips 12 --seconds 8 --holdout --seed 777

# 3) 数据集（按整段音频划分，避免相邻窗口泄漏）
.venv/bin/python scripts/make_dataset.py --src data/synth/train   --out artifacts/dataset_synth.npz
.venv/bin/python scripts/make_dataset.py --src data/synth/holdout --out artifacts/dataset_holdout.npz --no-split

# 4) 训练 + 导出 + 回验
.venv/bin/python scripts/train.py --data artifacts/dataset_synth.npz \
    --holdout artifacts/dataset_holdout.npz --out artifacts/model_v0.1
.venv/bin/python scripts/export_c.py --model artifacts/model_v0.1.pt \
    --header ../model-bench/main/listening_model.h \
    --vectors artifacts/dataset_synth.npz --vectors-per-class 3
c++ -std=c++17 -O2 -I ../model-bench/main scripts/host_check.cpp -o /tmp/host_check && /tmp/host_check

# 5) 跨实现特征校验（C++ 固件口径 vs Python 训练口径）
.venv/bin/python mac_demo/cross_check.py data/synth/train/street/street-007.wav
```

本分支以 ESP32-S3 能力为目标；电脑只用于训练、导出回验和编译，不把 Mac 麦克风结果当作
设备能力证据。

## 真实数据采集（下一步的关键）

```sh
IDF_PY=/Users/eranthe/.espressif/python_env/idf5.4_py3.12_env/bin/python
$IDF_PY scripts/collect_from_device.py --port /dev/cu.usbmodem101 \
    --label office --seconds 60 --out data/real/office-01
# 建议的标签（目录名前缀）：quiet / steady / street / horn / babble / near_voice / music / wind / other
$IDF_PY scripts/collect_from_device.py --convert data/real --out artifacts/dataset_real.npz
```

采集用的是设备 JSON 里的 24 频段值（与推理时同一份特征，零错位）。两处近似写在脚本头部：
broadband 用频段和近似、motion 固定 0；上固件时这两个字段各加一行 printf 即可消掉。

## 口径与证据纪律

* 合成语料上的准确率（验证 95.6% / 留出音色 94.7%）**只证明管线正确**，不证明真实场景表现。
* 已实测的短板：真实麦克风 10.7k/13.6kHz 频段能量高于合成语料（z ≈ +5.7），真实房间本底
  被判成 other。修法：真实录音重训 + 语料补高频本底。
* 设备端推理耗时**尚未实测**（未刷机）。刷机后固件会打印 p50/p95/max 与 27/27 自检结果。
* 时间特征在当前语料里无信息量（合成数据不注入时间相关性）；个人历史与用户纠正要靠真实
  数据积累，才谈得上"何时调、何时退出"。
