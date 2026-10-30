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

## QQ Music 实际流式测试

QQ Music 正在播放时运行：

```sh
sh tools/stem_remix/run_qq_stream.sh broadband
```

需要由 S3 实时切换策略并在上位机观察角色/风险时，使用：

```sh
cd tools
python3 -m host --system-eq-app com.tencent.QQMusicMac --stem-remix
```

该模式把 S3 的安静/人声/键盘/风/一般噪声映射为五种分轨策略；旧 24 段 EQ 自动归零，避免
与分轨处理叠加，S3 的媒体让位仍保留。歌曲内部角色仍由 Mac 上的六轨模型判断，因此这只是
“S3 选环境策略、Mac 选歌曲角色”的闭环，不代表 S3 固件已经理解歌曲分轨。

之后可直接在 QQ Music 切歌，不需要提前知道歌曲文件。Ctrl-C 结束后 process tap 会销毁，QQ Music
恢复原声。可选环境仍是 `quiet / broadband / keyboard / wind / babble`。

当前实现使用 2 秒窗口、0.5 秒重叠和 MPS；2026-10-29 在《月光》的 30 秒在线测试中，首次出声
约 3.11 秒，每窗处理约 0.80–1.10 秒，16 窗未出现 worker 错误或持续积压。这个结果只证明当前
Mac、当前歌曲片段和当前输出设备能够持续运行，不代表所有歌曲都达到相同分离质量。

首轮试听出现“人声疑似延迟 / 3D 合唱感”。根因候选是短窗六轨估算的相位与串轨误差被整轨
重组放大。流式混音已改成“原始音频作底 + 分轨增益差值”，并把默认策略强度设为 60%；修正版
20 秒在线测试首次出声约 2.63 秒、每窗约 0.88–1.19 秒，无 worker 错误和持续积压。听感改善仍
需要用户现场确认，运行指标不能替代听感结论。

后续多曲试听暴露“整体毛躁、鼓瞬态破碎、钢琴延音断点、混响大就糊”。定位到旧 worker 会把
每个 2 秒窗独立缩放到 0.98 峰值，造成跨窗响度抽动。现已删除每窗归一化，改为跨窗连续限幅；
策略也改为最高优先声部保持 0 dB、只衰减竞争声部，并以瞬态×声部密度×人声侧向能量构成风险
门，高风险自动回到原声。修正版 30 秒在线运行首次出声约 2.48 秒、每窗约 0.90–1.06 秒，输出
峰值约 0.95、无削波和持续积压；鼓、延音与毛躁的改善仍待用户听感确认。

测试：

```sh
python3 -m unittest tools/stem_remix/test_stem_remix.py
```
