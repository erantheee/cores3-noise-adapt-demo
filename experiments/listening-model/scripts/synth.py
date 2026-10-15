"""合成 9 类情境的 48 kHz 训练音频。

为什么先要合成：设备上的真实标注录音还不存在（采集工具 scripts/collect_from_device.py
已就位）。合成语料用于把"训练 → 导出 → 端侧推理"整条管线跑通、暴露结构性问题，并给出
一个可复现的基线。它的准确率**不代表真实场景**，真实录音进来后用同一条管线重训即可。

类目（id 与模型输出一致）：
    quiet      室内安静（本底）
    steady     稳定机械噪声（风扇/空调/电机嗡鸣）
    street     街道车流（户外交通，含车辆经过的起伏）
    horn       危险瞬态（鸣笛/警报；产品策略是"不增益"）
    babble     人声嘈杂（远处多人说话）
    near_voice 近场对话（有人在你面前说话）
    music      音乐泄漏/外放
    wind       风噪（ gusts 起伏）
    other      其他/混合（防止模型对陌生输入过度自信）

合成原则：所有素材都经过与固件相同的 48 kHz / 2560 帧分析管线，声源用的是"物理上说得通"
的近似（宽带噪声整形成分 + 真实 TTS 语音 + 用户自己的真实音乐片段），不引入任何在设备上
不存在的特征。
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import SAMPLE_RATE, load_wav_mono  # noqa: E402

CLASSES = ["quiet", "steady", "street", "horn", "babble",
           "near_voice", "music", "wind", "other"]

# 语音素材池：data/tts/<tag>/*.wav，每个文件是一个音色的一段带停顿长语音。
# 生成方式（需在沙箱外执行，sandbox 里 say 写不出音频）：
#   say -v 'Tingting' -r 170 -o data/tts/train/Tingting.wav \
#       --file-format=WAVE --data-format=LEI16@48000 '…句子…[[slnc 900]]…句子…'
# 训练用 6 个音色，验证用 1 个音色从未出现在训练里。

MUSIC_SOURCES = [
    Path("/Users/eranthe/Documents/Codex/2026-10-12/https-mimi-io-products-noise-adapt/work/dukou-48k.wav"),
    Path("/Users/eranthe/Documents/Codex/2026-10-12/https-mimi-io-products-noise-adapt/work/instrument-48k.wav"),
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


def rms_dbfs(x: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    return 20.0 * math.log10(max(rms, 1e-9))


def set_level(x: np.ndarray, target_db: float) -> np.ndarray:
    current = rms_dbfs(x)
    return x * db_to_gain(target_db - current)


def shaped_noise(rng: np.random.Generator, n: int, exponent: float,
                 f_lo: float = 20.0, f_hi: float = 20000.0) -> np.ndarray:
    """1/f^exponent 整形噪声（0=白，0.5=粉，1=棕），带通限制在 [f_lo, f_hi]。"""
    x = rng.standard_normal(n)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    freq_safe = np.maximum(freqs, 1.0)
    shape = (freq_safe / 1000.0) ** (-exponent)
    shape[freqs < f_lo] = 0.0
    shape[freqs > f_hi] = 0.0
    out = np.fft.irfft(spec * shape, n=n)
    return out / (np.abs(out).max() + 1e-9)


def emphasis(x: np.ndarray, center: float, width_oct: float, gain_db: float) -> np.ndarray:
    """在频域给某个中心频率附近加一个平滑的增益包络。"""
    spec = np.fft.rfft(x)
    freqs = np.maximum(np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE), 1.0)
    oct_dist = np.log2(freqs / center)
    bump = gain_db * np.exp(-0.5 * (oct_dist / width_oct) ** 2)
    return np.fft.irfft(spec * (10.0 ** (bump / 20.0)), n=len(x))


def gust_envelope(rng: np.random.Generator, n: int, rate_hz: float, depth_db: float) -> np.ndarray:
    """慢速起伏包络（阵风/车辆经过）：低通随机过程 + dB 映射。"""
    control = rng.standard_normal(max(8, int(n / SAMPLE_RATE * rate_hz * 8)))
    control = np.convolve(control, np.ones(9) / 9.0, mode="same")
    env = np.interp(np.linspace(0, len(control) - 1, n), np.arange(len(control)), control)
    env = env / (np.abs(env).max() + 1e-9) * (depth_db / 2.0)
    return 10.0 ** (env / 20.0)


def tone_burst(rng: np.random.Generator, n_samples: int, f0: float,
               harmonics: int = 3) -> np.ndarray:
    """喇叭/鸣笛音：少数谐波的复合音，快起慢收。"""
    t = np.arange(n_samples) / SAMPLE_RATE
    x = np.zeros(n_samples)
    for h in range(1, harmonics + 1):
        x += (1.0 / h) * np.sin(2 * np.pi * f0 * h * t + rng.uniform(0, 2 * np.pi))
    attack = min(int(0.012 * SAMPLE_RATE), n_samples // 4)
    env = np.ones(n_samples)
    env[:attack] = np.linspace(0, 1, attack)
    env[-attack:] = np.linspace(1, 0, attack)
    return x * env


def siren(rng: np.random.Generator, n_samples: int) -> np.ndarray:
    """警报：300-900 Hz 的连续扫频。"""
    t = np.arange(n_samples) / SAMPLE_RATE
    f_lo, f_hi, period = 320.0, 880.0, 1.6
    phase = 2 * np.pi * (f_lo * t + (f_hi - f_lo) * period / (2 * np.pi) *
                         (1 - np.cos(2 * np.pi * t / period)) / 2.0)
    return np.sin(phase)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """一阶低通（距离感/遮挡感）。"""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    spec *= 1.0 / (1.0 + (freqs / cutoff) ** 2)
    return np.fft.irfft(spec, n=len(x))


def microphone_floor(rng: np.random.Generator, n: int) -> np.ndarray:
    """麦克风/ADC 的高频底噪域随机化。

    首次 Mac 实时测试发现 10.7/13.6 kHz 两带比 v0.1 训练分布高约 4.7–5.7 sigma，
    安静房间因此被当成 ``other``。这不是一个应由策略层兜底的现象：同样的声学输入在
    不同麦克风、增益和供电下都会带有不同的高频噪声底。

    这里随机生成三种情况：低高频底噪、宽带高频底噪，以及带 10–14 kHz 电子噪声隆起
    的困难情况。它叠加到所有类别，因此模型不能再把麦克风响应误当作情境标签。
    """
    if rng.random() < 0.18:
        return np.zeros(n)
    floor = shaped_noise(rng, n, rng.uniform(-0.15, 0.25), 6500.0, 19000.0)
    if rng.random() < 0.65:
        floor = emphasis(floor, rng.uniform(10500.0, 14200.0),
                         rng.uniform(0.22, 0.55), rng.uniform(4.0, 11.0))
    return set_level(floor, rng.uniform(-62.0, -45.0))


def rain_room_tone(rng: np.random.Generator, n: int) -> np.ndarray:
    """所有场景共享的低电平房间与麦克风本底。"""
    acoustic = set_level(shaped_noise(rng, n, 0.6, 30.0, 15000.0),
                         rng.uniform(-60, -54))
    return acoustic + microphone_floor(rng, n)


def place(buf: np.ndarray, seg: np.ndarray, start_frac: float, gain_db: float | None = None):
    """把一段音频混入缓冲区（环绕处理）。"""
    n = len(buf)
    start = int(start_frac * n) % max(n, 1)
    idx = (np.arange(len(seg)) + start) % n
    np.add.at(buf, idx, seg if gain_db is None else seg * db_to_gain(gain_db))


# ---------------------------------------------------------------------------
# 语音素材池：整段长语音 -> 按停顿切成短句
# ---------------------------------------------------------------------------

def split_utterances(x: np.ndarray, thresh_db: float = -52.0,
                     min_len_s: float = 0.45, pad_s: float = 0.06) -> list[np.ndarray]:
    """按电平门把一段语音切成短句（含少量边沿余量）。"""
    hop = int(0.02 * SAMPLE_RATE)
    n_blocks = max(len(x) // hop, 1)
    frames = x[:n_blocks * hop].reshape(n_blocks, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    active = rms > 10 ** (thresh_db / 20.0)
    chunks = []
    start = None
    for i, on in enumerate(active):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if (i - start) * 0.02 >= min_len_s:
                a = max(int((start - pad_s / 0.02) * hop), 0)
                b = min(int((i + pad_s / 0.02) * hop), len(x))
                chunks.append(x[a:b])
            start = None
    if start is not None and (len(active) - start) * 0.02 >= min_len_s:
        chunks.append(x[start * hop:])
    # 合并过短的碎片，避免把爆破音当成一句话
    merged = []
    for c in chunks:
        if merged and len(c) < int(0.25 * SAMPLE_RATE):
            merged[-1] = np.concatenate([merged[-1], c])
        else:
            merged.append(c)
    return merged


def voice_pool(root: Path, tag: str) -> list[list[np.ndarray]]:
    """每个音色一条长语音 -> 该音色的一组短句。"""
    files = sorted((root / tag).glob("*.wav"))
    if not files:
        raise SystemExit(f"缺少语音素材池 {root / tag}（见文件头部的生成说明）")
    pool = [split_utterances(load_wav_mono(f).astype(np.float64)) for f in files]
    if any(len(p) == 0 for p in pool):
        raise SystemExit(f"{root / tag} 中有文件切不出短句，检查电平门")
    return pool


# ---------------------------------------------------------------------------
# 各情境生成器（返回 [-1,1] 的 float 数组，长度约 seconds 秒）
# ---------------------------------------------------------------------------

def gen_quiet(rng, n, voices, music):
    room = rain_room_tone(rng, n)
    room *= gust_envelope(rng, n, 0.05, rng.uniform(1.5, 4.0))
    return room


def gen_steady(rng, n, voices, music):
    base = shaped_noise(rng, n, 0.85, 40.0, 6000.0)
    base = emphasis(base, rng.uniform(180, 320), 1.6, rng.uniform(3, 7))
    hum = 0.4 * np.sin(2 * np.pi * 100 * np.arange(n) / SAMPLE_RATE)
    hum *= 0.3 + 0.7 * np.abs(gust_envelope(rng, n, 0.02, 0.5))
    mix = set_level(base, rng.uniform(-40, -33)) + set_level(hum, -52)
    return mix + rain_room_tone(rng, n)


def gen_street(rng, n, voices, music):
    base = shaped_noise(rng, n, 0.9, 30.0, 9000.0)
    base = emphasis(base, 140.0, 1.8, 6.0)
    base = set_level(base, rng.uniform(-42, -35))
    events = rng.integers(1, 4)
    mix = base.copy()
    for _ in range(events):
        length = int(rng.uniform(1.2, 3.0) * SAMPLE_RATE)
        if length >= n:
            length = n
        seg = shaped_noise(rng, length, 0.7, 60.0, 4000.0)
        seg = emphasis(seg, 220.0, 1.2, 5.0)
        seg = set_level(seg, rng.uniform(-40, -33))
        seg *= np.hanning(length)
        place(mix, seg, rng.uniform(0, 1))
    return mix + rain_room_tone(rng, n)


def gen_horn(rng, n, voices, music):
    mix = gen_street(rng, n, voices, music)
    mix = set_level(mix, rng.uniform(-48, -42))
    for _ in range(int(rng.integers(1, 4))):
        if rng.random() < 0.45:
            length = int(rng.uniform(1.0, 2.0) * SAMPLE_RATE)
            seg = siren(rng, length)
            seg = set_level(seg, rng.uniform(-16, -11))
        else:
            length = int(rng.uniform(0.35, 1.1) * SAMPLE_RATE)
            f0 = rng.uniform(380, 560)
            seg = tone_burst(rng, length, f0, harmonics=int(rng.integers(2, 4)))
            seg = set_level(seg, rng.uniform(-18, -12))
        place(mix, seg, rng.uniform(0, 1))
    return mix


def gen_babble(rng, n, voices, music):
    k = int(rng.integers(4, 8))
    mix = np.zeros(n)
    for _ in range(k):
        speaker = voices[int(rng.integers(0, len(voices)))]
        stream = np.zeros(n)
        pos = int(rng.uniform(0, 0.6 * n))
        while pos < n:
            utt = speaker[int(rng.integers(0, len(speaker)))]
            take = lowpass(utt, rng.uniform(3200, 5200))     # 距离感
            end = min(pos + len(take), n)
            stream[pos:end] += take[:end - pos]
            pos = end + int(rng.uniform(0.05, 0.55) * SAMPLE_RATE)
        level = rng.uniform(-40, -33) - 10 * math.log10(k / 3.0)
        mix += set_level(stream, level)
    return mix + rain_room_tone(rng, n)


def gen_near_voice(rng, n, voices, music):
    speaker = voices[int(rng.integers(0, len(voices)))]
    stream = np.zeros(n)
    pos = int(rng.uniform(0, 0.12 * SAMPLE_RATE))
    spoken = 0
    while pos < n and spoken < 5:
        utt = speaker[int(rng.integers(0, len(speaker)))]
        take = lowpass(utt, rng.uniform(6500, 8500))
        end = min(pos + len(take), n)
        stream[pos:end] += take[:end - pos]
        pos = end + int(rng.uniform(0.12, 0.55) * SAMPLE_RATE)
        spoken += 1
    if float(np.abs(stream).max()) < 1e-6:
        return rain_room_tone(rng, n)
    mix = set_level(stream, rng.uniform(-29, -23))
    return mix + rain_room_tone(rng, n)


def gen_music(rng, n, voices, music):
    src = music[int(rng.integers(0, len(music)))]
    start = int(rng.uniform(0, max(len(src) - n, 1)))
    seg = src[start:start + n]
    if len(seg) < n:
        seg = np.pad(seg, (0, n - len(seg)))
    mix = set_level(seg, rng.uniform(-48, -38))
    return mix + rain_room_tone(rng, n)


def gen_wind(rng, n, voices, music):
    base = shaped_noise(rng, n, 1.1, 25.0, 3000.0)
    base = emphasis(base, 90.0, 1.3, 7.0)
    base = set_level(base, rng.uniform(-48, -40))
    base *= gust_envelope(rng, n, rng.uniform(0.2, 0.7), rng.uniform(5, 12))
    hiss = shaped_noise(rng, n, 0.2, 1500.0, 9000.0)
    hiss = set_level(hiss, rng.uniform(-52, -44))
    hiss *= gust_envelope(rng, n, rng.uniform(0.3, 0.9), rng.uniform(4, 9))
    return base + hiss + rain_room_tone(rng, n)


def gen_other(rng, n, voices, music):
    makers = [gen_babble, gen_music, gen_street, gen_steady, gen_near_voice]
    a = makers[int(rng.integers(0, len(makers)))](rng, n, voices, music)
    b = makers[int(rng.integers(0, len(makers)))](rng, n, voices, music)
    mix = set_level(a, rng.uniform(-46, -36)) + set_level(b, rng.uniform(-46, -38))
    if rng.random() < 0.25:
        mix += music[0][:n] * db_to_gain(rng.uniform(-52, -46))
    return mix + rain_room_tone(rng, n)


GENERATORS = {
    "quiet": gen_quiet, "steady": gen_steady, "street": gen_street, "horn": gen_horn,
    "babble": gen_babble, "near_voice": gen_near_voice, "music": gen_music,
    "wind": gen_wind, "other": gen_other,
}


def write_wav(path: Path, samples: np.ndarray):
    data = np.clip(samples, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def build(root: Path, tts_root: Path, clips_per_class: int, seconds: float, seed: int,
          holdout: bool):
    voices = voice_pool(tts_root, "holdout" if holdout else "train")
    music = [load_wav_mono(p).astype(np.float64) for p in MUSIC_SOURCES if p.exists()]
    if not music:
        raise SystemExit("找不到音乐源文件（MUSIC_SOURCES）")
    n = int(seconds * SAMPLE_RATE)
    tag = "holdout" if holdout else "train"
    for ci, cls in enumerate(CLASSES):
        gen = GENERATORS[cls]
        for k in range(clips_per_class):
            rng = np.random.default_rng(seed + 1000 * ci + k)
            clip = gen(rng, n, voices, music)
            peak = np.abs(clip).max()
            if peak > 0.99:
                clip = clip / peak * 0.99
            write_wav(root / tag / cls / f"{cls}-{k:03d}.wav", clip)
        print(f"{tag}/{cls}: {clips_per_class} clips")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/synth")
    ap.add_argument("--tts-root", default="data/tts")
    ap.add_argument("--clips", type=int, default=60)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args()
    base = Path(__file__).resolve().parent.parent
    build(base / args.root, base / args.tts_root, args.clips, args.seconds, args.seed,
          args.holdout)


if __name__ == "__main__":
    main()
