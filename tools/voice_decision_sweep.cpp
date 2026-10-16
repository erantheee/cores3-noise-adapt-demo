// 扫"哼唧声 / 呼吸声"在语音判据下会拿到多少分，找各自的最坏情况。
//
// **必须是序列模式，不能逐帧独立跑。** 判据从 D15 起带时域闸门，靠跨帧历史判断
// "这个声音是不是一直在那儿不动"。给每一帧都开一个全新的 NoiseAnalyzer，历史永远是空、
// 闸门永远返回 1.0 —— 那样量到的是改造前的行为，工具会骗人（第一版就是这么写的）。
//
// 所以这里每个参数组合都生成一段连续信号，按固件的帧长切块，**同一个 analyzer 顺序喂**，
// 再统计这一段里的分数分布。
//
// 用法：
//   g++ -std=c++17 -O2 -I main -o /tmp/voicsweep tools/voice_decision_sweep.cpp main/controller.cpp
//   /tmp/voicsweep

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <numeric>
#include <random>
#include <string>
#include <vector>
#include "controller.hpp"

namespace {
constexpr float kSampleRate = 48000.0f;
/// 与固件一致：512 样本一块，攒 5 块 = 2560 后分析。
constexpr std::size_t kFrame = 2560;
constexpr float kSeconds = 4.0f;
constexpr float kVoiceCut = 0.55f;

struct Resonator {
    float b0 = 0, a1 = 0, a2 = 0, z1 = 0, z2 = 0;
    void set(float f, float bw) {
        const float r = std::exp(-float(M_PI) * bw / kSampleRate);
        const float th = 2.0f * float(M_PI) * f / kSampleRate;
        a1 = -2.0f * r * std::cos(th);
        a2 = r * r;
        b0 = (1.0f - r) * std::sqrt(1.0f - 2.0f * r * std::cos(2.0f * th) + r * r);
    }
    float process(float x) {
        const float y = b0 * x - a1 * z1 - a2 * z2;
        z2 = z1;
        z1 = y;
        return y;
    }
};

void normalise_band(std::vector<float>& x, float lo, float hi, float target) {
    double power = 0.0;
    int bins = 0;
    for (float f = lo; f <= hi; f *= 1.15f) {
        const float w = 2.0f * float(M_PI) * f / kSampleRate;
        const float c = 2.0f * std::cos(w);
        float s1 = 0, s2 = 0;
        for (std::size_t i = 0; i < x.size(); ++i) {
            const float s0 = x[i] + c * s1 - s2;
            s2 = s1;
            s1 = s0;
        }
        power += std::max(s1 * s1 + s2 * s2 - c * s1 * s2, 1.0e-20f);
        ++bins;
    }
    const float rms = std::sqrt(static_cast<float>(power / std::max(bins, 1))) /
                      static_cast<float>(x.size());
    const float gain = std::pow(10.0f, (target - 20.0f * std::log10(std::max(rms, 1.0e-9f))) / 20.0f);
    for (float& v : x) v *= gain;
}

std::vector<int16_t> quantise(const std::vector<float>& x, float dbfs) {
    float peak = 1.0e-9f;
    for (float v : x) peak = std::max(peak, std::abs(v));
    const float gain = std::pow(10.0f, dbfs / 20.0f) / peak;
    std::vector<int16_t> out(x.size());
    for (std::size_t i = 0; i < x.size(); ++i)
        out[i] = static_cast<int16_t>(
            std::lround(std::max(-1.0f, std::min(1.0f, x[i] * gain)) * 32767.0f));
    return out;
}

// 哼唧：闭口发声。声门谐波过两个低频共振峰，**全程不做任何调制**——这正是它和说话的结构差别。
std::vector<float> hum(float f0, float roll, float r1f, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    const std::size_t n = static_cast<std::size_t>(kSeconds * kSampleRate);
    std::vector<float> out(n);
    Resonator r1, r2;
    r1.set(r1f, 260.0f);
    r2.set(r1f * 2.4f, 420.0f);
    const int harmonics = static_cast<int>(3500.0f / f0);
    for (std::size_t i = 0; i < n; ++i) {
        const float t = static_cast<float>(i) / kSampleRate;
        float g = 0.0f;
        for (int h = 1; h <= harmonics; ++h)
            g += std::sin(2.0f * float(M_PI) * f0 * h * t) / std::pow(static_cast<float>(h), roll);
        g += 0.02f * normal(rng);
        out[i] = r1.process(g) + 0.5f * r2.process(g);
    }
    normalise_band(out, 300.0f, 4000.0f, -20.0f);
    return out;
}

// 呼吸：湍流噪声，按呼吸周期起落。没有谐波结构。
std::vector<float> breath(float hp, float rumble_mix, float rate, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    const std::size_t n = static_cast<std::size_t>(kSeconds * kSampleRate);
    std::vector<float> out(n);
    float hpf = 0, prev = 0, rumble = 0;
    for (std::size_t i = 0; i < n; ++i) {
        const float t = static_cast<float>(i) / kSampleRate;
        const float nz = normal(rng);
        hpf = hp * (hpf + nz - prev);
        prev = nz;
        rumble = 0.995f * rumble + 0.005f * nz;
        const float env = 0.5f - 0.5f * std::cos(2.0f * float(M_PI) * rate * t);
        out[i] = (hpf + rumble_mix * rumble) * env;
    }
    normalise_band(out, 300.0f, 4000.0f, -20.0f);
    return out;
}

struct Row {
    float median = 0, worst = 0;
    int over = 0, frames = 0;
    std::string tag;
};

Row measure(const std::vector<float>& sig, float dbfs, const std::string& tag) {
    const auto pcm = quantise(sig, dbfs);
    NoiseAnalyzer analyzer;   // 一个序列一个实例：闸门靠它的历史
    Row row;
    row.tag = tag;
    const std::size_t frames = pcm.size() / kFrame;
    row.frames = static_cast<int>(frames);
    std::vector<float> scores;
    scores.reserve(frames);
    for (std::size_t k = 0; k < frames; ++k) {
        const auto f = analyzer.analyze(pcm.data() + k * kFrame, kFrame,
                                        static_cast<uint32_t>(k * 100));
        scores.push_back(f.voice_likelihood);
        if (f.voice_likelihood >= kVoiceCut) ++row.over;
    }
    if (!scores.empty()) {
        std::vector<float> sorted = scores;
        std::sort(sorted.begin(), sorted.end());
        row.median = sorted[sorted.size() / 2];
        row.worst = sorted.back();
    }
    return row;
}

void summarise(const std::string& title, std::vector<Row> rows) {
    std::sort(rows.begin(), rows.end(),
              [](const Row& a, const Row& b) { return a.worst > b.worst; });
    const int any_over = static_cast<int>(std::count_if(
        rows.begin(), rows.end(), [](const Row& r) { return r.over > 0; }));
    std::printf("\n=== %s ===\n", title.c_str());
    std::printf("扫了 %zu 种；有帧越线（>=%.2f）的 %d 种\n", rows.size(), kVoiceCut, any_over);
    std::printf("按最高分排前 5：\n");
    for (int i = 0; i < 5 && i < static_cast<int>(rows.size()); ++i) {
        const auto& r = rows[i];
        std::printf("  最高=%.4f 中位=%.4f 越线 %d/%d 帧   %s%s\n", r.worst, r.median, r.over,
                    r.frames, r.tag.c_str(), r.over > 0 ? "   <<< 有帧越线" : "");
    }
}
}  // namespace

int main() {
    std::printf("语音判据 kVoiceCut = %.2f，序列模式（%.1f 秒/组，%zu 样本/帧）\n",
                kVoiceCut, kSeconds, kFrame);

    std::vector<Row> hums;
    for (float f0 : {90.0f, 110.0f, 130.0f, 150.0f, 170.0f, 190.0f, 200.0f, 210.0f,
                     230.0f, 250.0f, 280.0f, 320.0f})
        for (float roll : {0.8f, 1.0f, 1.2f, 1.5f, 1.8f, 2.2f})
            for (float r1 : {250.0f, 400.0f, 600.0f})
                for (float db : {-40.0f, -30.0f, -20.0f})
                    hums.push_back(measure(hum(f0, roll, r1, 7001), db,
                                           "f0=" + std::to_string(int(f0)) + " roll=" +
                                               std::to_string(roll).substr(0, 3) + " F1=" +
                                               std::to_string(int(r1)) + " " +
                                               std::to_string(int(db)) + "dB"));
    summarise("哼唧声", hums);

    std::vector<Row> breaths;
    for (float hp : {0.70f, 0.75f, 0.80f, 0.83f, 0.86f, 0.88f, 0.90f, 0.93f, 0.96f, 0.99f})
        for (float mix : {0.0f, 0.3f, 0.8f, 1.5f, 3.0f})
            for (float rate : {0.4f, 0.6f, 1.0f})
                for (float db : {-40.0f, -30.0f, -20.0f})
                    breaths.push_back(measure(breath(hp, mix, rate, 8001), db,
                                              "hp=" + std::to_string(hp).substr(0, 4) +
                                                  " rumble=" + std::to_string(mix).substr(0, 3) +
                                                  " " + std::to_string(int(db)) + "dB"));
    summarise("呼吸声", breaths);
    return 0;
}
