// Tests for main/dual_mic.hpp: two-microphone frame statistics (D47).
//
// Why this suite exists: the firmware now reads the ES7210's two mics as interleaved
// L/R samples and reports per-frame statistics (right-mic level, level difference,
// Pearson coherence) that are meant to separate near-field speech from far-field
// room sound. Every one of those quantities is plain arithmetic, but a sign error
// (L-R backwards), a squaring error (energy vs mean) or an overflow (int16 sums in
// int16) would silently poison the calibration data that later decisions are built
// on. These tests pin the maths on synthetic interleaved data with known answers.
#include <cmath>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

#include "../main/dual_mic.hpp"

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const std::string& name) {
    if (condition) {
        ++passed;
        std::cout << "PASS " << name << '\n';
    } else {
        ++failed;
        std::cout << "FAIL " << name << '\n';
    }
}

bool close_enough(float actual, float expected, float tolerance = 1.0e-2f) {
    return std::fabs(static_cast<double>(actual) - static_cast<double>(expected)) <= tolerance;
}

/// Build interleaved L/R data from two mono tracks.
std::vector<int16_t> interleave(const std::vector<int16_t>& l, const std::vector<int16_t>& r) {
    std::vector<int16_t> out;
    out.reserve(l.size() * 2);
    for (std::size_t i = 0; i < l.size(); ++i) {
        out.push_back(l[i]);
        out.push_back(r[i]);
    }
    return out;
}

/// 1 kHz sine at 48 kHz, `amplitude` in full-scale fraction, `frames` samples long.
std::vector<int16_t> sine(float amplitude, float phase_deg, std::size_t frames) {
    std::vector<int16_t> out(frames);
    const float phase = phase_deg * 3.14159265358979f / 180.0f;
    for (std::size_t i = 0; i < frames; ++i) {
        const float t = static_cast<float>(i) / 48000.0f;
        out[i] = static_cast<int16_t>(
            std::lround(amplitude * 32767.0f * std::sin(2.0f * 3.14159265358979f * 1000.0f * t + phase)));
    }
    return out;
}

}  // namespace

int main() {
    using namespace dual_mic;

    // ---- 1) Identical channels: coherence ≈ 1, diff ≈ 0, levels match ----
    {
        const auto l = sine(0.5f, 0.0f, 512);
        const auto stereo = interleave(l, l);
        Accumulator acc;
        acc.push(stereo.data(), 512);
        const Stats stats = finish(acc);
        check(stats.valid, "相同双路样本：统计有效");
        // 0.5 full-scale sine: RMS = 0.5/sqrt(2) ≈ 0.3536 -> -9.03 dBFS.
        check(close_enough(stats.left_dbfs, -9.03f, 0.05f),
              "0.5 满幅正弦电平 ≈ -9.03 dBFS");
        check(close_enough(stats.diff_db, 0.0f, 0.05f), "相同信号电平差 ≈ 0");
        check(close_enough(stats.coherence, 1.0f, 1.0e-3f), "相同信号相关系数 ≈ 1");
    }

    // ---- 2) Inverted channels: coherence ≈ -1 ----
    {
        const auto l = sine(0.5f, 0.0f, 512);
        const auto r = sine(0.5f, 180.0f, 512);
        const auto stereo = interleave(l, r);
        Accumulator acc;
        acc.push(stereo.data(), 512);
        const Stats stats = finish(acc);
        check(stats.valid, "反相双路样本：统计有效");
        check(close_enough(stats.coherence, -1.0f, 1.0e-3f), "反相信号相关系数 ≈ -1");
    }

    // ---- 3) Half-amplitude right channel: diff ≈ +6.02 dB (L louder) ----
    {
        const auto l = sine(0.5f, 0.0f, 512);
        const auto r = sine(0.25f, 0.0f, 512);
        const auto stereo = interleave(l, r);
        Accumulator acc;
        acc.push(stereo.data(), 512);
        const Stats stats = finish(acc);
        // 0.25 full-scale sine: RMS = 0.25/sqrt(2) -> -15.05 dBFS.
        check(close_enough(stats.right_dbfs, -15.05f, 0.05f), "半幅右路电平 ≈ -15.05 dBFS");
        check(close_enough(stats.diff_db, 6.02f, 0.05f), "电平差 = 左 − 右 ≈ +6.02 dB");
    }

    // ---- 4) Independent noise channels: |coherence| small ----
    {
        std::vector<int16_t> stereo(512 * 2);
        uint32_t seed = 7;
        for (std::size_t i = 0; i < 512; ++i) {
            seed = seed * 1664525u + 1013904223u;
            stereo[2 * i] = static_cast<int16_t>((seed >> 16) & 0xFFFF);
            seed = seed * 1664525u + 1013904223u;
            stereo[2 * i + 1] = static_cast<int16_t>((seed >> 16) & 0xFFFF);
        }
        Accumulator acc;
        acc.push(stereo.data(), 512);
        const Stats stats = finish(acc);
        check(stats.valid, "独立噪声：统计有效");
        check(std::fabs(static_cast<double>(stats.coherence)) < 0.25f,
              "独立噪声 |相关系数| < 0.25（实测样本量下的波动上限）");
    }

    // ---- 5) Silence: valid, floor level, zero difference ----
    {
        std::vector<int16_t> stereo(512 * 2, 0);
        Accumulator acc;
        acc.push(stereo.data(), 512);
        const Stats stats = finish(acc);
        check(stats.valid, "静音帧也算有效（有样本）");
        check(stats.left_dbfs <= -100.0f, "静音电平落在测量地板");
        check(close_enough(stats.diff_db, 0.0f, 0.01f), "静音电平差 ≈ 0");
    }

    // ---- 6) Empty accumulator: invalid, defaults intact ----
    {
        Accumulator acc;
        const Stats stats = finish(acc);
        check(!stats.valid, "空累加器：valid=false");
        check(stats.left_dbfs == -120.0f && stats.right_dbfs == -120.0f,
              "空累加器：电平保持默认 -120");
    }

    // ---- 7) reset() clears; push(nullptr) is a no-op ----
    {
        const auto l = sine(0.5f, 0.0f, 512);
        const auto stereo = interleave(l, l);
        Accumulator acc;
        acc.push(stereo.data(), 512);
        acc.reset();
        check(!finish(acc).valid, "reset() 后统计无效");
        acc.push(nullptr, 512);
        check(!finish(acc).valid, "push(nullptr) 不产生样本");
    }

    // ---- 8) Multiple blocks accumulate: 4×512 = one 2048-frame analysis window ----
    {
        const auto l = sine(0.5f, 0.0f, 512);
        const auto stereo = interleave(l, l);
        Accumulator acc;
        for (int block = 0; block < 4; ++block) acc.push(stereo.data(), 512);
        const Stats stats = finish(acc);
        check(stats.valid, "四块累计：valid");
        check(close_enough(stats.coherence, 1.0f, 1.0e-3f), "四块累计：相关系数仍 ≈ 1");
    }

    std::cout << "\n" << passed << " 条通过、" << failed << " 条失败\n";
    return failed == 0 ? 0 : 1;
}
