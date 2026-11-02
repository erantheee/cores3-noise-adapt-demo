# 多轨角色重混 Demo

这条链路验证的不是“把人声抬高”，而是：歌曲先估算成六轨，再把乐器轨映射成主导、节奏、
低频和背景角色，最后按环境需求重新混合。它与 `tools/systemeq` 独立，不会把离线结果冒充成
QQ 音乐实时执行。

六轨模型输出：`vocals / drums / bass / guitar / piano / other`。`other` 仍可能混有弦乐、
管乐、合成器和效果声；这些是模型估算，不是录音棚原始分轨。

```sh
python3 tools/stem_remix/stem_remix.py /path/to/song.mp3 \
  --work-dir /tmp/stem-remix-work \
  --output-dir /tmp/stem-remix-output
```

已经有六轨 WAV 时，可跳过分离：

```sh
python3 tools/stem_remix/stem_remix.py /path/to/song.mp3 \
  --stems-dir /path/to/stems \
  --work-dir /tmp/stem-remix-work \
  --output-dir /tmp/stem-remix-output
```

输出一个六轨零增益重组基准，以及四种策略：主导保护、节奏保护、低频保护、自动角色保护；
所有版本统一到约 −14 LUFS，并限制真峰值。零增益重组用于先暴露分轨伪影，它不是未经处理的
原始音频。自动角色目前使用整曲能量启发式，不是逐时间片识别，这一限制会写入 manifest。

逐时间片版本每 2 秒重新判断主导、节奏、低频和背景角色；新角色需要至少领先 2 dB 且满足
保持窗，输出再用 250 ms 过渡，避免能量轻微波动造成策略抽动。环境输入沿用 S3 可表达的语义：
`quiet / broadband / keyboard / wind / babble`。

```sh
uv run --python 3.11 --with numpy --with soundfile \
  tools/stem_remix/dynamic_remix.py \
  --stems-dir /path/to/stems \
  --environment broadband \
  --work-wav /tmp/dynamic-remix.wav \
  --output /tmp/dynamic-remix.mp3
```

`dynamic_remix.py` 本身仍是离线渲染；实时路径见下一节。

## QQ Music 实时分轨（默认链路）

QQ Music 正在播放时，正常启动上位机：

```sh
sh tools/host_up.sh
```

默认同时开启四轨实时分轨。需要观察原始四轨/六轨链路时可以单独启动：

```sh
sh tools/stem_remix/run_qq_stream.sh broadband
```

四轨模型是 `htdemucs`，输出 `vocals / drums / bass / other`；吉他和钢琴不是单独轨。
需要六轨时显式指定：

```sh
cd tools
python3 -m host --system-eq-app com.tencent.QQMusicMac --stem-model htdemucs_6s
```

该模式把 S3 的安静/一般噪声/键盘/风/背景人声映射为五种策略；旧 24 段 EQ 自动归零，
避免与分轨处理叠加，S3 的媒体让位仍保留。歌曲内部角色由 Mac 上的模型判断，因此这是
“S3 选环境策略、Mac 选歌曲角色”的闭环，不代表 S3 固件在端上分轨。

当前默认参数为 2 s 输入窗、0.1 s overlap、Demucs 内部 2 s 前向、MPS float16
autocast。2026-10-30 用《渡口》30 s 做 worker 测速，17 个窗口平均 **1.40 s**、
最大 **1.78 s**，平均 RTF **0.70**；输出 hop 为 1.75 s，12 s ring 能吸收偶发抖动。
首次出声仍要等一个输入窗加一次推理，约 **3–4 s**，因此歌词/视频同步问题并没有消失。
这证明当前 Mac 上四轨能在 30 s 样本持续处理，不证明所有歌曲的分离质量或长期稳定性。

内部 `--segment` 必须与实时输入窗一起设置。旧版只把 worker 的输入改成 2 s，Demucs
仍按模型默认约 7.8 s 前向，导致“短窗”没有真正降低计算量。四轨模型缺失的
`guitar/piano` 在共享策略结构里按静音处理，混音时不会伪造不存在的轨道。

听感仍未验收：短窗模型估算可能有相位/串轨、鼓瞬态、延音与混响伪影；运行指标不能替代
现场听感。需要回到无分轨主链时传 `--no-stem-remix`。

测试：

```sh
python3 -m unittest tools/stem_remix/test_stem_remix.py
```

## 2026-11-02 候选补充

关闭EQ与断流后的中性处方会请求 worker 输出捕获的原始采样，状态以 `stem_bypass`
回执为准。队列、窗口和交叉淡化延迟仍在，不能说是即时原声；组件测试不代表现场听感。
