#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>

#include "controller.hpp"
#include "listening_model.h"

/// 2 秒（20 帧 × 100 ms 节拍）滑窗 → 92 维模型输入。
///
/// 权威定义是 `experiments/listening-model/scripts/features.py::window_vector()`——训练用的
/// 就是那个函数。这里逐项复刻它。**为什么必须复刻而不是"差不多"**：口径错位不会报错，
/// 模型照样给出一个类别，只是安静地变差。v0.2-s3 上已经量到两处这类错位，都写在下面，
/// 都是数字不是感觉。
///
/// 与 `experiments/ear-context/main/window.hpp` 有两处**刻意不同**：
///
/// 1. **`voice_likelihood` 用未过时域闸门的逐帧乘积。**
///    `NoiseAnalyzer::analyze()` 返回的 `frame.voice_likelihood` 现在乘了
///    `modulation_term()`（2026-10-16 加入，见 controller.cpp 的 31/32 项）。训练时**没有**
///    这道闸门——features.py 的 `voice_likelihood()` 就是四个判据的乘积。窗里有 2 维吃这个
///    量（`vl_mean`/`vl_std`），喂闸门后的值就是分布外输入。所以这里显式调用
///    `voice_likelihood_from_features(extract_voice_features(frame))` 取逐帧乘积。
///    （ear-context 那套喂的是闸门后的值，属同一类错位。）
///
/// 2. **时间 5 维用 `when=None` 的默认基线（12:00、周三），不是"周一 00:00"。**
///    训练给每条 clip 抽了随机时间（`make_dataset.py::random_when`），所以这 5 维在训练集里
///    是**有分布的**（σ = 0.28–0.77，不是常量），`collect_from_device.py` 采真实数据时用的
///    也是 `when=None`。ear-context 硬编码的"周一 00:00"与训练口径归一化后最大差 **4.04σ**：
///    拿 model-bench 的 27 条自检向量实测，**3 条改了 argmax**。
///
/// 3. 运动 2 维保持 0：v0.2 是 `motion=None` 训的，填真六轴就是分布外输入
///    （见 integration-plan.md 决策 2）。这条是硬的。
///
/// 这个头文件不依赖 ESP-IDF，主机可以编译并跑单测（`test/context_window_test.cpp`）。
namespace context {

/// 窗长。features.py 的 `WINDOW_FRAMES`。
constexpr std::size_t kWindowFrames = 20;
/// features.py 的 `FEATURE_COUNT`。
constexpr std::size_t kFeatureCount = 92;

static_assert(kFeatureCount == listening_model::kFeatureCount,
              "窗特征维数与 listening_model.h 不一致：重跑 export_c.py 或改 features.py");

/// 缺失时钟基线：与 features.py 的 `when=None` 默认值逐项一致。
/// hour_frac = 0.5 → 12:00；dow = 2 → 周三（Python 的 weekday() 从周一开始）。
/// 设备没有同步时钟，这是"声明没有钟"，**不是**测到的墙上时间。
constexpr double kMissingHourFrac = 0.5;
constexpr std::size_t kMissingDayOfWeek = 2;

/// 窗里的一帧：只保留模型真正消费的量，推理时不再回头碰音频。
struct ContextFrame {
    std::array<float, kBandCount> band_dbfs{};
    float broadband_dbfs = 0.0f;
    /// 逐帧语音乘积（未过时域闸门）——见文件头第 1 条。
    float voice_product = 0.0f;
    float spectral_flatness = 0.0f;
    float vocal_peakiness = 0.0f;
    float vocal_ratio = 0.0f;
    float excited_vocal_bands = 0.0f;
    /// 帧时刻（秒）。斜率只用它与窗内均值的差，绝对值不影响结果。
    double t_s = 0.0;
};

/// 一次前向的结果由 `ContextResult`（controller.hpp）承载：它随 `EarbudCommand` 一起
/// 下发给屏幕与串口，所以定义在那边，不在这里重复一份。

/// 固定 20 帧的滑窗，无动态内存。对象约 2.6 KB——仍然别放在任务栈上
/// （`processing_task` 只有 12 KB 栈）。
class ContextWindow {
public:
    /// 清空。输入中断（麦克风失效、开始录音）时必须调用：否则断点前后的音频会被当成
    /// 一段连续声音，斜率和帧间差都会变成假的。
    void reset() {
        count_ = 0;
        next_ = 0;
    }

    bool full() const { return count_ == kWindowFrames; }
    std::size_t size() const { return count_; }

    void push(const ContextFrame& frame) {
        frames_[next_] = frame;
        next_ = (next_ + 1) % kWindowFrames;
        if (count_ < kWindowFrames) ++count_;
    }

    /// 把窗口压成 92 维向量。**只有满窗才有定义**（训练只喂满窗）：不满时返回 false，
    /// 且不写 `out`。不写半窗的统计量，是为了不让"没攒够"这条路径悄悄产生一个可上报的值。
    bool build_vector(float* out) const;

private:
    /// 第 i 帧，i = 0 是最旧的一帧。
    const ContextFrame& at(std::size_t i) const {
        const std::size_t oldest = full() ? next_ : 0;
        return frames_[(oldest + i) % kWindowFrames];
    }

    std::array<ContextFrame, kWindowFrames> frames_{};
    std::size_t count_ = 0;
    std::size_t next_ = 0;
};

namespace detail {
/// 一列的均值与 **population** 标准差（除以 n）。numpy 的 `ndarray.std()` 默认也是
/// population；换成样本标准差（n−1）就会与训练差一点点，而"差一点点"正是这里最贵的错。
inline void mean_and_std(const std::array<double, kWindowFrames>& v, double* mean,
                         double* stddev) {
    constexpr double kFrames = static_cast<double>(kWindowFrames);
    *mean = 0.0;
    for (double x : v) *mean += x / kFrames;
    double variance = 0.0;
    for (double x : v) {
        const double d = x - *mean;
        variance += d * d / kFrames;
    }
    *stddev = std::sqrt(variance);
}
}  // namespace detail

inline bool ContextWindow::build_vector(float* out) const {
    if (!full()) return false;
    constexpr double kFrames = static_cast<double>(kWindowFrames);
    constexpr double kPi = 3.14159265358979323846;

    double t_mean = 0.0;
    for (std::size_t i = 0; i < kWindowFrames; ++i) t_mean += at(i).t_s / kFrames;

    // 24 个频段：均值 / 标准差 / 对时间的斜率。
    for (std::size_t band = 0; band < kBandCount; ++band) {
        std::array<double, kWindowFrames> v{};
        for (std::size_t i = 0; i < kWindowFrames; ++i) v[i] = at(i).band_dbfs[band];
        double mean = 0.0, stddev = 0.0;
        detail::mean_and_std(v, &mean, &stddev);
        double covariance = 0.0;
        double t_variance = 0.0;
        for (std::size_t i = 0; i < kWindowFrames; ++i) {
            const double dt = at(i).t_s - t_mean;
            covariance += dt * (v[i] - mean);
            t_variance += dt * dt;
        }
        out[band] = static_cast<float>(mean);
        out[24 + band] = static_cast<float>(stddev);
        out[48 + band] = static_cast<float>(t_variance > 0.0 ? covariance / t_variance : 0.0);
    }

    std::array<double, kWindowFrames> level{};
    std::array<double, kWindowFrames> voice{};
    double flatness = 0.0;
    double peakiness = 0.0;
    double vocal_ratio = 0.0;
    double excited = 0.0;
    std::size_t active = 0;
    std::size_t transitions = 0;
    std::size_t steps = 0;
    for (std::size_t i = 0; i < kWindowFrames; ++i) {
        const ContextFrame& record = at(i);
        level[i] = record.broadband_dbfs;
        voice[i] = record.voice_product;  // 第 1 条：未过闸门的逐帧乘积
        if (voice[i] > 0.55) ++active;
        if (i > 0) {
            if ((voice[i] > 0.55) != (voice[i - 1] > 0.55)) ++transitions;
            if (std::fabs(level[i] - level[i - 1]) > 3.0) ++steps;
        }
        flatness += record.spectral_flatness / kFrames;
        peakiness += record.vocal_peakiness / kFrames;
        vocal_ratio += record.vocal_ratio / kFrames;
        excited += static_cast<double>(record.excited_vocal_bands) / kFrames;
    }

    double level_mean = 0.0, level_std = 0.0;
    detail::mean_and_std(level, &level_mean, &level_std);
    double level_min = level[0];
    double level_max = level[0];
    for (double x : level) {
        if (x < level_min) level_min = x;
        if (x > level_max) level_max = x;
    }
    double voice_mean = 0.0, voice_std = 0.0;
    detail::mean_and_std(voice, &voice_mean, &voice_std);

    out[72] = static_cast<float>(level_mean);
    out[73] = static_cast<float>(level_std);
    out[74] = static_cast<float>(level_min);
    out[75] = static_cast<float>(level_max);
    out[76] = static_cast<float>(voice_mean);
    out[77] = static_cast<float>(voice_std);
    out[78] = static_cast<float>(active) / kFrames;
    out[79] = static_cast<float>(transitions);
    out[80] = static_cast<float>(flatness);
    out[81] = static_cast<float>(peakiness);
    out[82] = static_cast<float>(vocal_ratio);
    out[83] = static_cast<float>(excited);
    out[84] = static_cast<float>(steps);
    // 运动两维：v0.2 是 motion=None 训的，必须是 0。
    out[85] = 0.0f;
    out[86] = 0.0f;
    // 时间五维：见文件头第 2 条。用训练那组公式现算，不写近似小数。
    out[87] = static_cast<float>(std::sin(2.0 * kPi * kMissingHourFrac));
    out[88] = static_cast<float>(std::cos(2.0 * kPi * kMissingHourFrac));
    out[89] = static_cast<float>(
        std::sin(2.0 * kPi * static_cast<double>(kMissingDayOfWeek) / 7.0));
    out[90] = static_cast<float>(
        std::cos(2.0 * kPi * static_cast<double>(kMissingDayOfWeek) / 7.0));
    out[91] = kMissingDayOfWeek >= 5 ? 1.0f : 0.0f;
    return true;
}

/// 数值稳定的 softmax 全分布：probs[c] = exp(logit_c − logit_max) / Σ，和 = 1。
///
/// D46（混合环境识别）：环境音多数时候是混合的（近处人声+音乐、街道+持续噪声……），
/// argmax 单标签把"第二名还占了多少"丢掉，混合场景看起来就像"模型很确定"。上报全分布
/// 的 top1 + top2 之后，"混合"才能被显式建模与度量，而不是被单标签藏起来。
inline void softmax_probs(const float* logits, std::size_t count, float* probs) {
    float best = logits[0];
    for (std::size_t c = 1; c < count; ++c) {
        if (logits[c] > best) best = logits[c];
    }
    double sum = 0.0;
    for (std::size_t c = 0; c < count; ++c) {
        probs[c] = static_cast<float>(
            std::exp(static_cast<double>(logits[c] - best)));
        sum += static_cast<double>(probs[c]);
    }
    for (std::size_t c = 0; c < count; ++c) {
        probs[c] = static_cast<float>(static_cast<double>(probs[c]) / sum);
    }
}

/// 第二候选概率达到该门槛即判"混合"。9 类均匀分布时 top2 ≈ 0.111，单类占 0.9 时
/// top2 ≈ 0.05；0.20 表示第二名至少占 1/5——混合成分强到不该被单标签藏起来。
constexpr float kMixedTop2Threshold = 0.20f;

/// argmax + 该类的 softmax 置信度，与 `ear-context/main/app_main.cpp` 同式：
/// score = 1 / Σ exp(logit_i − logit_best)。保持原实现（count 通用，不限于 9 类）。
inline void top_class(const float* logits, std::size_t count, std::size_t* index, float* score) {
    std::size_t best = 0;
    for (std::size_t c = 1; c < count; ++c) {
        if (logits[c] > logits[best]) best = c;
    }
    double sum = 0.0;
    for (std::size_t c = 0; c < count; ++c) {
        sum += std::exp(static_cast<double>(logits[c] - logits[best]));
    }
    *index = best;
    *score = static_cast<float>(1.0 / sum);
}

/// 前向耗时的滚动分位数（最近 256 次）。方案与 `latency::nearest_rank_percentile` 一致：
/// **最近秩**，报出来的每个数都是一次真实观测，不是插值出来的。
///
/// 单独一个计数器而不是塞进 `latency::LatencyProfiler`：那张表的 7 个阶段是"能量依次流过的
/// 通路"，接口和上位机都按那 7 个名字对齐；为了一个推理耗时改阶段表，会把面板、
/// 协议解析和它们的测试一起拖进来。这里只要 p50/p95/max。
class ContextTiming {
public:
    void record(float us) {
        if (!std::isfinite(us) || us < 0.0f) return;
        samples_[next_] = us;
        next_ = (next_ + 1) % kCapacity;
        if (count_ < kCapacity) ++count_;
    }

    std::size_t samples() const { return count_; }

    /// `fraction` ∈ [0, 1]。空集合返回 0。
    float percentile(float fraction) const {
        if (count_ == 0) return 0.0f;
        for (std::size_t i = 0; i < count_; ++i) scratch_[i] = samples_[i];
        std::sort(scratch_.begin(), scratch_.begin() + static_cast<std::ptrdiff_t>(count_));
        const float clamped = std::fmin(std::fmax(fraction, 0.0f), 1.0f);
        std::size_t rank = static_cast<std::size_t>(
            std::ceil(static_cast<double>(clamped) * static_cast<double>(count_)));
        if (rank == 0) rank = 1;
        if (rank > count_) rank = count_;
        return scratch_[rank - 1];
    }

    float max_us() const { return percentile(1.0f); }

    void reset() {
        count_ = 0;
        next_ = 0;
    }

private:
    static constexpr std::size_t kCapacity = 256;
    std::array<float, kCapacity> samples_{};
    /// 排序用副本，放在对象里而不是栈上：调用点在 `processing_task`（12 KB 栈）。
    mutable std::array<float, kCapacity> scratch_{};
    std::size_t count_ = 0;
    std::size_t next_ = 0;
};

}  // namespace context
