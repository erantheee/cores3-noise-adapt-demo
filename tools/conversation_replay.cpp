// 把一段 WAV 按固件节拍喂进分析器 + 控制器，看 Apple 式对话感知在这段真实音频上
// 什么时候触发、压到多深、什么时候恢复。
//
// 为什么是 2560 样本一拍：固件的音频循环每拍只分析一帧 2560 样本（5 x 512），控制节拍
// 100 ms（main/app_main.cpp 的 kControlPeriodMs 与 kFrameSamples）。这里逐字照搬，
// 所以"第 N 拍触发"与真机上的第 N 拍是同一个刻度。重采样到 48 kHz 是因为分析器的
// 24 段中心频率按 48 kHz 标定；素材一般是 16 kHz，插值上来即可（判据只看 300-4000 Hz）。
//
// **素材口径（两条，都会让结论外推失效）**：
//   1. 记录/声音类别/ 下的素材是 **Mac 麦克风** 录的（状态总表第 38h 项），判据阈值是在
//      设备 ES7210 上标定的，所以那批素材只能验证"触发与时序"的形状；
//   2. 设备端录音只有 **16 kHz**（串口 48 kHz 需要 128 kB/s，可用吞吐 92 kB/s，物理上传不了），
//      重采样到 48 kHz 后 8 kHz 以上是空的：**涉及高频段的判据会失真**。
//      工具会对低于 48 kHz 的素材显式警告，不要拿这类结论去改设备判据。
//
// 用法：
//   g++ -std=c++17 -O2 -I main -o /tmp/convreplay tools/conversation_replay.cpp \
//       main/controller.cpp main/conversation_awareness.cpp
//   /tmp/convreplay 记录/声音类别/speech/20261016-speech-01.wav
//   /tmp/convreplay <wav> --row        # 只打一行汇总，便于批量扫

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "controller.hpp"

namespace {

constexpr std::size_t kFrameSamples = 2560;   // 与固件一致
constexpr float kTickMs = 100.0f;             // kControlPeriodMs
constexpr float kTargetRate = 48000.0f;

struct Wav {
    std::vector<int16_t> samples;
    std::uint32_t rate = 0;
    std::size_t frames = 0;
};

std::uint32_t le32(const unsigned char* p) {
    return static_cast<std::uint32_t>(p[0]) | (static_cast<std::uint32_t>(p[1]) << 8) |
           (static_cast<std::uint32_t>(p[2]) << 16) | (static_cast<std::uint32_t>(p[3]) << 24);
}
std::uint16_t le16(const unsigned char* p) {
    return static_cast<std::uint16_t>(p[0]) | (static_cast<std::uint16_t>(p[1]) << 8);
}

bool load_wav(const char* path, Wav& out, std::string& error) {
    FILE* f = std::fopen(path, "rb");
    if (f == nullptr) { error = "打不开文件"; return false; }
    std::vector<unsigned char> bytes;
    unsigned char buffer[65536];
    std::size_t got = 0;
    while ((got = std::fread(buffer, 1, sizeof(buffer), f)) > 0) bytes.insert(bytes.end(), buffer, buffer + got);
    std::fclose(f);
    if (bytes.size() < 44 || std::memcmp(bytes.data(), "RIFF", 4) != 0 ||
        std::memcmp(bytes.data() + 8, "WAVE", 4) != 0) {
        error = "不是 RIFF/WAVE";
        return false;
    }
    std::size_t pos = 12;
    std::uint16_t channels = 0, bits = 0, format = 0;
    while (pos + 8 <= bytes.size()) {
        const unsigned char* chunk = bytes.data() + pos;
        const std::uint32_t size = le32(chunk + 4);
        const std::size_t body = pos + 8;
        if (std::memcmp(chunk, "fmt ", 4) == 0 && body + 16 <= bytes.size()) {
            format = le16(bytes.data() + body);
            channels = le16(bytes.data() + body + 2);
            out.rate = le32(bytes.data() + body + 4);
            bits = le16(bytes.data() + body + 14);
        } else if (std::memcmp(chunk, "data", 4) == 0) {
            const std::size_t end = std::min(body + size, bytes.size());
            if (format != 1 || bits != 16 || channels == 0) { error = "只支持 PCM16"; return false; }
            const std::size_t total = (end - body) / 2;
            out.frames = total / channels;
            out.samples.resize(out.frames);
            for (std::size_t i = 0; i < out.frames; ++i) {
                const unsigned char* p = bytes.data() + body + i * channels * 2;
                out.samples[i] = static_cast<int16_t>(le16(p));
            }
        }
        pos = body + size + (size & 1u);
    }
    if (out.frames == 0 || out.rate == 0) { error = "没有 data 块或采样率缺失"; return false; }
    return true;
}

/// 线性插值到 48 kHz。素材是 16 kHz，只用来喂判据（300-4000 Hz），插值精度足够。
std::vector<int16_t> resample(const Wav& wav) {
    if (std::fabs(static_cast<float>(wav.rate) - kTargetRate) < 1.0f) return wav.samples;
    const double ratio = kTargetRate / static_cast<double>(wav.rate);
    const std::size_t out_count = static_cast<std::size_t>(wav.frames * ratio);
    std::vector<int16_t> out(out_count, 0);
    for (std::size_t i = 0; i < out_count; ++i) {
        const double src = i / ratio;
        const std::size_t i0 = static_cast<std::size_t>(src);
        const std::size_t i1 = std::min(i0 + 1, wav.frames - 1);
        const double frac = src - static_cast<double>(i0);
        const double value = wav.samples[i0] * (1.0 - frac) + wav.samples[i1] * frac;
        out[i] = static_cast<int16_t>(std::max(-32768.0, std::min(32767.0, value)));
    }
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf("用法: convreplay <wav> [--row]\n");
        return 1;
    }
    const bool row_only = (argc > 2 && std::string(argv[2]) == "--row");

    Wav wav;
    std::string error;
    if (!load_wav(argv[1], wav, error)) {
        std::printf("读取失败: %s\n", error.c_str());
        return 2;
    }
    const std::vector<int16_t> audio = resample(wav);
    const double audio_seconds = static_cast<double>(wav.frames) / wav.rate;
    if (wav.rate < 48000) {
        std::printf("⚠️ 素材是 %u Hz：重采样到 48 kHz 后 8 kHz 以上无内容，"
                    "涉及高频段的判据会失真，结论不可直接外推到设备。\n", wav.rate);
    }

    NoiseAnalyzer analyzer;
    AdaptController controller;
    // 回放工具要检验的正是**近场代理**在真实素材上的行为（它是判断"证据该不该换"的依据），
    // 所以这里显式选 near_field_demo 策略；产品默认是 bone_only（fail-closed，不动作）。
    controller.set_evidence_policy(EvidencePolicy::near_field_demo);
    std::printf("（已打开近场代理兜底：本工具测的就是代理路径，不是产品默认行为）\n");

    std::size_t offset = 0;
    int tick = 0;
    int duck_start = -1, duck_end = -1;
    int own_voice_start = -1;
    float own_voice_peak = 0.0f;
    int min_percent = 100;
    int speech_ticks = 0;
    int engaged_ticks = 0;
    bool saw_speech = false;

    while (offset + kFrameSamples <= audio.size()) {
        ++tick;
        const std::uint32_t ts = static_cast<std::uint32_t>(tick * kTickMs);
        AnalysisFrame frame = analyzer.analyze(audio.data() + offset, kFrameSamples, ts);
        const EarbudCommand command = controller.update(frame, kTickMs);
        const conversation::Output& aware = controller.awareness();

        if (frame.voice_likelihood > 0.55f) { saw_speech = true; ++speech_ticks; }
        if (aware.engaged) ++engaged_ticks;
        own_voice_peak = std::max(own_voice_peak, aware.own_voice);
        if (own_voice_start < 0 && aware.engaged) own_voice_start = tick;
        if (command.media_percent < 99 && duck_start < 0) duck_start = tick;
        if (duck_start >= 0 && command.media_percent >= 99 && duck_end < 0) duck_end = tick;
        min_percent = std::min(min_percent, static_cast<int>(command.media_percent));

        if (!row_only && (aware.state != conversation::State::idle || command.media_percent < 99)) {
            std::printf("tick %4d  t=%6.2fs  level=%7.1f dB  voice=%.2f  own=%.2f  %-10s media=%3u%%  %s\n",
                        tick, tick * kTickMs / 1000.0, frame.broadband_dbfs, frame.voice_likelihood,
                        aware.own_voice, conversation::state_name(aware.state),
                        static_cast<unsigned>(command.media_percent),
                        conversation_state_name(command.conversation));
        }
        offset += kFrameSamples;
    }

    const double tick_seconds = tick * kTickMs / 1000.0;
    if (row_only) {
        std::printf("%s|%.2f|%d|%.2f|%d|%d|%d|%.2f|%d|%d|%s\n", argv[1], audio_seconds, tick,
                    tick_seconds, duck_start, duck_end, min_percent, own_voice_peak, speech_ticks,
                    engaged_ticks,
                    duck_start >= 0 ? "DUCKED" : (saw_speech ? "SPEECH_NO_DUCK" : "NO_SPEECH"));
    } else {
        std::printf("\n音频 %.2f s（%u Hz）→ %d 拍（%.2f s 控制时间）\n", audio_seconds, wav.rate, tick,
                    tick_seconds);
        std::printf("语音出现过: %s\n", saw_speech ? "是" : "否");
        std::printf("own_voice 峰值: %.2f\n", own_voice_peak);
        std::printf("语音拍数: %d / %d，已下压拍数: %d\n", speech_ticks, tick, engaged_ticks);
        if (duck_start >= 0) {
            std::printf("下压: 第 %d 拍开始（t=%.2fs），最低 %d%%%s\n", duck_start,
                        duck_start * kTickMs / 1000.0, min_percent,
                        duck_end >= 0 ? "" : "，素材结束前未恢复");
            if (duck_end >= 0) {
                std::printf("恢复: 第 %d 拍（t=%.2fs）回到 100%%，本次下压 %.2f s\n", duck_end,
                            duck_end * kTickMs / 1000.0, (duck_end - duck_start) * kTickMs / 1000.0);
            }
        } else {
            std::printf("下压: 未触发\n");
        }
    }
    return 0;
}
