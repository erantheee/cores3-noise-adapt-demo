#include "bone_voice.hpp"

#include <algorithm>
#include <cmath>

namespace bone {
namespace {

constexpr float kBucketMs = 100.0f;
/// 运动判据的时间常数。**不能太长**：走动时要在半秒内就被认出来（那半秒宁可不触发），
/// 2 s 的一阶滤波在 2.4 s 后只走到 70%，门限 0.35 根本摸不到 —— 这是测试抓出来的。
constexpr float kMotionTauMs = 500.0f;

float clamp01(float value) { return std::clamp(value, 0.0f, 1.0f); }

/// Pearson 相关系数。窗内任一侧方差为 0 时返回 0：常量（例如安静到没有起伏）不构成证据，
/// 这一条是"静音不能触发"的第一道闸。
float correlation(const float* a, const float* b, std::size_t count) {
    if (count < 2) return 0.0f;
    double mean_a = 0.0, mean_b = 0.0;
    for (std::size_t i = 0; i < count; ++i) { mean_a += a[i]; mean_b += b[i]; }
    mean_a /= static_cast<double>(count);
    mean_b /= static_cast<double>(count);
    double cov = 0.0, var_a = 0.0, var_b = 0.0;
    for (std::size_t i = 0; i < count; ++i) {
        const double da = a[i] - mean_a;
        const double db = b[i] - mean_b;
        cov += da * db;
        var_a += da * da;
        var_b += db * db;
    }
    if (var_a <= 1e-12 || var_b <= 1e-12) return 0.0f;
    return static_cast<float>(cov / std::sqrt(var_a * var_b));
}

}  // namespace

void BoneVoice::push_imu(float accel_magnitude, float t_ms) {
    // 用真实时间戳算采样间隔：ODR 从 100 Hz 提到 400 Hz 时基线常数不用重调。
    const float dt_s = (last_t_ms_ >= 0.0f) ? std::max((t_ms - last_t_ms_) / 1000.0f, 1e-4f)
                                            : 0.01f;
    last_t_ms_ = t_ms;
    if (!gravity_valid_) {
        gravity_ = accel_magnitude;
        gravity_valid_ = true;
    } else {
        const float tau = std::max(params_.gravity_tau_s, 1e-3f);
        const float alpha = 1.0f - std::exp(-dt_s / tau);
        gravity_ += (accel_magnitude - gravity_) * alpha;
    }
    const float vibration = accel_magnitude - gravity_;
    bucket_sq_ += static_cast<double>(vibration) * static_cast<double>(vibration);
    ++bucket_count_;
}

Output BoneVoice::push_audio(float speech_band_dbfs, float t_ms) {
    (void)t_ms;
    Output out;
    if (bucket_count_ == 0) return out;

    const float vib_rms = static_cast<float>(std::sqrt(bucket_sq_ / static_cast<double>(bucket_count_)));
    bucket_sq_ = 0.0;
    bucket_count_ = 0;

    // 慢窗运动判据：走动、转头、敲桌子都会让振动变大，但和音频无关。
    const float motion_alpha = 1.0f - std::exp(-kBucketMs / kMotionTauMs);
    motion_rms_ += (vib_rms - motion_rms_) * motion_alpha;
    const bool motion = motion_rms_ > params_.motion_veto;

    vib_window_[window_next_] = vib_rms;
    audio_window_[window_next_] = std::pow(10.0f, speech_band_dbfs / 20.0f);
    window_next_ = (window_next_ + 1) % kMaxWindow;
    if (window_count_ < kMaxWindow) ++window_count_;

    out.gravity = gravity_;
    out.vibration_rms = vib_rms;
    out.motion = motion;
    out.ready = window_count_ >= params_.min_pairs;
    if (!out.ready) return out;

    // 按时间顺序复制出窗口，环形的顺序不能直接喂给相关系数。
    float vib[kMaxWindow];
    float audio[kMaxWindow];
    const std::size_t count = window_count_;
    for (std::size_t i = 0; i < count; ++i) {
        const std::size_t index = (window_next_ + kMaxWindow - count + i) % kMaxWindow;
        vib[i] = vib_window_[index];
        audio[i] = audio_window_[index];
    }

    out.correlation = correlation(vib, audio, count);
    const float corr_term = clamp01((out.correlation - params_.corr_min) /
                                    std::max(params_.corr_full - params_.corr_min, 1e-3f));
    const float vib_term = clamp01(vib_rms / std::max(params_.vibration_full, 1e-6f));
    out.confidence = motion ? 0.0f : corr_term * vib_term;
    return out;
}

void BoneVoice::reset() {
    gravity_valid_ = false;
    gravity_ = 0.0f;
    last_t_ms_ = -1.0f;
    bucket_sq_ = 0.0;
    bucket_count_ = 0;
    window_count_ = 0;
    window_next_ = 0;
    motion_rms_ = 0.0f;
}

}  // namespace bone
