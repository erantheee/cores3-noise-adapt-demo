// 把一段真实 WAV 喂进固件里那份语音判据，看它逐帧怎么判、会不会触发调音。
//
// 为什么需要它：合成信号只能证明机制的**形状**，证明不了覆盖。要回答"真实的清嗓子会不会
// 被当成语音"，只能拿真人录音过一遍真判据。这个程序链接的就是 main/controller.cpp。
//
// 用法：
//   g++ -std=c++17 -O2 -I main -o /tmp/vdoffline tools/voice_decision_offline.cpp main/controller.cpp
//   /tmp/vdoffline <wav> [每秒几帧]
//
// ⚠️ 麦克风口径：本机 Mac 麦和设备的 ES7210 频响不同，而判据的阈值是在设备上标定的。
//    用 Mac 麦录的素材可以判断**机制**对不对，不能直接搬阈值。要设备口径就得让
//    ear-context 固件把 48 kHz 原始音频吐出来（现在是 16 kHz 的语音中继速率，不够）。

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>
#include "controller.hpp"

namespace {
constexpr float kTargetRate = 48000.0f;
constexpr std::size_t kFrame = 2560;   // 与固件一致：512×5
constexpr float kVoiceCut = 0.55f;

uint32_t rd32(const uint8_t* p) { return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24); }
uint16_t rd16(const uint8_t* p) { return uint16_t(uint16_t(p[0]) | (uint16_t(p[1]) << 8)); }

/// 只支持 PCM16 的 RIFF/WAVE；返回单声道 float。
bool load_wav(const std::string& path, std::vector<float>& out, int& rate) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { std::fprintf(stderr, "打不开 %s\n", path.c_str()); return false; }
    std::vector<uint8_t> blob((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (blob.size() < 44 || std::memcmp(blob.data(), "RIFF", 4) || std::memcmp(blob.data() + 8, "WAVE", 4)) {
        std::fprintf(stderr, "%s 不是 RIFF/WAVE\n", path.c_str()); return false;
    }
    const uint8_t* fmt = nullptr; std::size_t fmt_len = 0;
    const uint8_t* data = nullptr; std::size_t data_len = 0;
    std::size_t pos = 12;
    while (pos + 8 <= blob.size()) {
        const uint8_t* id = blob.data() + pos;
        const uint32_t size = rd32(blob.data() + pos + 4);
        const std::size_t body = pos + 8;
        if (body + size > blob.size()) break;
        if (!std::memcmp(id, "fmt ", 4)) { fmt = blob.data() + body; fmt_len = size; }
        else if (!std::memcmp(id, "data", 4)) { data = blob.data() + body; data_len = size; }
        pos = body + size + (size & 1);
    }
    if (!fmt || !data || fmt_len < 16) { std::fprintf(stderr, "缺少 fmt/data 块\n"); return false; }
    const uint16_t tag = rd16(fmt);
    const uint16_t channels = rd16(fmt + 2);
    const uint32_t sample_rate = rd32(fmt + 4);
    const uint16_t bits = rd16(fmt + 14);
    if (tag != 1 || bits != 16) {
        std::fprintf(stderr, "只支持 PCM16，这个文件 tag=%u bits=%u。先转成 16 bit PCM WAV。\n", tag, bits);
        return false;
    }
    if (channels < 1 || channels > 8) { std::fprintf(stderr, "声道数不合理 %u\n", channels); return false; }

    const std::size_t frames = data_len / (2 * channels);
    std::vector<float> mono(frames);
    for (std::size_t i = 0; i < frames; ++i) {
        int acc = 0;
        for (uint16_t c = 0; c < channels; ++c) {
            acc += int16_t(rd16(data + (i * channels + c) * 2));
        }
        mono[i] = float(acc) / float(channels) / 32768.0f;
    }
    // 线性插值重采样到 48 kHz。判据的频段中心是按 48 kHz 定的，改采样率等于换特征。
    if (uint32_t(sample_rate) != uint32_t(kTargetRate)) {
        const double ratio = kTargetRate / double(sample_rate);
        const std::size_t n = std::size_t(double(frames) * ratio);
        std::vector<float> resampled(n);
        for (std::size_t i = 0; i < n; ++i) {
            const double src = double(i) / ratio;
            const std::size_t i0 = std::size_t(src);
            const std::size_t i1 = std::min(i0 + 1, frames ? frames - 1 : 0);
            const float t = float(src - double(i0));
            resampled[i] = frames ? mono[i0] * (1.0f - t) + mono[i1] * t : 0.0f;
        }
        out.swap(resampled);
    } else {
        out.swap(mono);
    }
    rate = int(kTargetRate);
    return true;
}
}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "用法: %s <wav> [每秒几帧]\n", argv[0]); return 2; }
    const int frames_per_sec = argc > 2 ? std::atoi(argv[2]) : 10;

    std::vector<float> mono; int rate = 0;
    if (!load_wav(argv[1], mono, rate)) return 2;

    std::vector<int16_t> pcm(mono.size());
    for (std::size_t i = 0; i < mono.size(); ++i)
        pcm[i] = int16_t(std::lround(std::max(-1.0f, std::min(1.0f, mono[i])) * 32767.0f));

    const std::size_t frames = pcm.size() / kFrame;
    std::printf("%s\n%.2f s @ %d Hz（已重采样到 48 kHz），%zu 帧 × %zu 样本\n\n",
                argv[1], double(mono.size()) / kTargetRate, rate, frames, kFrame);
    if (frames == 0) { std::fprintf(stderr, "太短\n"); return 2; }

    NoiseAnalyzer analyzer;
    AdaptController controller;
    std::vector<float> score(frames);
    std::vector<int> mode(frames);
    int crossings = 0, triggers = 0, first_trigger = -1;
    double worst = 0.0;
    std::size_t worst_at = 0;

    for (std::size_t k = 0; k < frames; ++k) {
        const auto frame = analyzer.analyze(pcm.data() + k * kFrame, kFrame,
                                            uint32_t(k * 1000 / frames_per_sec));
        score[k] = frame.voice_likelihood;
        if (frame.voice_likelihood >= kVoiceCut) ++crossings;
        if (frame.voice_likelihood > worst) { worst = frame.voice_likelihood; worst_at = k; }
        const auto cmd = controller.update(frame, 1000.0f / float(frames_per_sec));
        mode[k] = int(cmd.tuning_mode);
        if (cmd.tuning_mode == TuningMode::external_voice_intent) {
            ++triggers;
            if (first_trigger < 0) first_trigger = int(k * 1000 / frames_per_sec);
        }
    }

    // 最高分那一帧的四个描述量：判断是**哪一道闸门**没拦住，比只看分数有用得多。
    {
        NoiseAnalyzer probe;   // 新实例 = 只看单帧判据本身
        const auto frame = probe.analyze(pcm.data() + worst_at * kFrame, kFrame, 0);
        const auto vf = extract_voice_features(frame);
        std::printf("最高分那一帧的描述量（门槛在括号里）\n");
        std::printf("  vocal_ratio   %.3f   (>=0.34)\n", vf.vocal_ratio);
        std::printf("  peakiness     %.3f   (<=0.72)\n", vf.vocal_peakiness);
        std::printf("  flatness      %.3f   (<=0.45)\n", vf.spectral_flatness);
        std::printf("  excited_bands %.0f     (>=5)\n", double(vf.excited_vocal_bands));
        std::printf("  level_gate    %.3f\n", vf.level_gate);
        std::printf("  broadband     %.1f dBFS\n\n", frame.broadband_dbfs);
    }

    // 时间轴：每格 0.5 s，显示该格内最高分
    const int per_cell = std::max(1, frames_per_sec / 2);
    std::printf("时间轴（每格 0.5 s，数字=该格内最高 voice_likelihood，* = 越线 %.2f）\n", kVoiceCut);
    for (std::size_t start = 0; start < frames; start += per_cell) {
        const std::size_t end = std::min(frames, start + per_cell);
        float peak = 0.0f;
        for (std::size_t k = start; k < end; ++k) peak = std::max(peak, score[k]);
        std::printf("%6.1fs %5.2f%s\n", double(start) / frames_per_sec, peak,
                    peak >= kVoiceCut ? " *" : "");
    }

    std::printf("\n汇总\n");
    std::printf("  越线帧      %d / %zu  (%.1f%%)\n", crossings, frames,
                100.0 * double(crossings) / double(frames));
    std::printf("  最高分      %.4f  (第 %.1f s)\n", worst, double(worst_at) / frames_per_sec);
    std::printf("  触发调音    %d 帧%s\n", triggers,
                first_trigger >= 0 ? ("，最早 " + std::to_string(first_trigger) + " ms").c_str() : "");
    return 0;
}
