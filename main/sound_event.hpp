#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>

/// 端侧「声音事件」判断：安静 / 说话 / 敲键盘 / 风 / 撞击。
///
/// 与 9 类听音情境模型（`listening_model.h`）是**两条独立的判断**：
///
/// * 9 类模型吃 2 秒 × 92 维窗，回答"这是什么场景"；它没有键盘类，且真实风判不出来。
/// * 这里吃 1 秒 × 15 维窗，回答"此刻有没有敲键盘/吹风这类瞬态事件"，**只报不决策**。
///
/// 依据（2026-10-19 真机采集，`记录/键盘风声/`）：
/// * 键盘：低频能量占比低（broadband 咔哒）、中频段帧间标准差大、频谱质心高；
/// * 风：频段斜率更负、低频段抬高、低频包络起伏大，且语音带峰值集中度低（不像说话）；
/// * 说话：语音带峰值集中度最高——这条把"说话"从风和键盘里分出来；
/// * 撞击：麦克风削波（`mic_valid=false`）→ 直接报 `artifact` 并清窗，绝不当成键盘。
///
/// 训练/评估脚本在 `experiments/sound-events/`；本文件的阈值必须与
/// `evaluate.py` 的 `Rule` / `Hold` 默认值逐字段一致（`test/sound_event_test.cpp` 用
/// 同一份真机窗口做金标准）。
///
/// 设计纪律：
/// * **不引入新 DSP。** 只消费现有 24 段 dBFS 与宽带电平，描述量是它们的初等函数。
/// * 头文件不依赖 ESP-IDF，主机可以编译并跑单测。
/// * 噪声地板用"向下快、向上慢"的跟踪器，不用"安静段中位数"——固件开机时不知道哪段安静。
namespace sound_event {

constexpr std::size_t kBandCount = 24;
/// 事件窗：10 帧 × 100 ms = 1 s。比 9 类模型的 2 s 窗短，因为键盘/阵风都是短事件。
constexpr std::size_t kWindowFrames = 10;

/// 24 段中心频率，与 `controller.cpp` / `experiments/listening-model/scripts/features.py`
/// 逐值一致。
constexpr std::array<float, kBandCount> kBandCenterHz = {
    70.7f, 141.4f, 244.9f, 346.4f, 451.7f, 566.8f, 696.5f, 841.7f,
    996.8f, 1171.3f, 1370.0f, 1595.0f, 1854.7f, 2154.1f, 2503.3f,
    2916.3f, 3414.7f, 4036.1f, 4829.1f, 5824.1f, 7020.0f, 8552.8f,
    10677.1f, 13638.2f,
};

/// 低频 / 中频 / 高频段的频段边界（不含端点）：[0,5)、[5,16)、[16,24)。
constexpr std::size_t kLowEnd = 5;
constexpr std::size_t kMidEnd = 16;

enum class Class : std::uint8_t {
    quiet = 0,
    speech = 1,
    keyboard = 2,
    wind = 3,
    /// 输入中断（削波/非有限值），常见原因是设备被撞到硬物。不是声音类别。
    artifact = 4,
};

inline const char* class_name(Class value) {
    switch (value) {
        case Class::quiet: return "quiet";
        case Class::speech: return "speech";
        case Class::keyboard: return "keyboard";
        case Class::wind: return "wind";
        case Class::artifact: return "artifact";
    }
    return "quiet";
}

/// 阈值。默认值与 `experiments/sound-events/evaluate.py` 的 `Rule` 一致。
struct Config {
    // 噪声地板跟踪（每秒 10 帧）：向下 0.25、向上 0.002。
    float floor_down = 0.25f;
    float floor_up = 0.002f;
    // 键盘：低频能量占比 < 0.55、中频帧间标准差 > 4 dB、频谱质心 > 600 Hz、
    // 且（频谱斜率 > -1.35 或质心 > 800 Hz）——阵风也有大 std_mid，但斜率更陡。
    float keyboard_low_share = 0.55f;
    float keyboard_std_mid = 4.0f;
    float keyboard_cent = 600.0f;
    /// 键盘的频谱斜率下限：阵风也有大 std_mid，但斜率更陡（≤ −1.45）。
    float keyboard_slope = -1.35f;
    /// 质心高于这个值时跳过斜率检查：强宽带咔哒本来就该过。
    float keyboard_cent_escape = 800.0f;
    // 风：频段斜率 < -1.45、语音带峰值集中度 < 0.45、低频相对抬升 > 16 dB、低频包络起伏 > 14 dB。
    float wind_slope = -1.45f;
    float wind_vocal_peak = 0.45f;
    float wind_d_lo = 16.0f;
    float wind_range_lo = 14.0f;
    // 说话：语音带峰值集中度 > 0.45。
    float speech_vocal_peak = 0.45f;
    // 状态保持（秒）：避免 1 s 窗逐帧切换。
    float hold_keyboard_s = 0.8f;
    float hold_wind_s = 2.0f;
    float hold_speech_s = 0.8f;
    /// 撞击后的保持时间：这段时间内不把后续帧当成正常事件（窗在重新攒）。
    float hold_artifact_s = 2.0f;
};

/// 15 维事件特征。顺序与 `features.py::FEATURE_NAMES` 逐项一致。
struct Features {
    float d_lo = 0.0f;
    float d_mid = 0.0f;
    float d_hi = 0.0f;
    float mid_lo = 0.0f;
    float hi_mid = 0.0f;
    float cent = 0.0f;
    float flat = 0.0f;
    float vr = 0.0f;
    float std_lo = 0.0f;
    float range_lo = 0.0f;
    float std_mid = 0.0f;
    float level_rel = 0.0f;
    float vocal_peak = 0.0f;
    float slope = 0.0f;
    float low_share = 0.0f;
};

constexpr std::size_t kFeatureCount = 15;
static_assert(kFeatureCount == 15, "Features 字段数与 FEATURE_NAMES 不一致");

/// 窗里的一帧：24 段 dBFS + 逐帧描述量。描述量在 push 时算一次，窗里取均值。
struct WindowFrame {
    std::array<float, kBandCount> band_dbfs{};
    float broadband_dbfs = -120.0f;
    float centroid = 0.0f;
    float flatness = 0.0f;
    float vocal_ratio = 0.0f;
    float vocal_peak = 0.0f;
    float slope = 0.0f;
    float low_share = 0.0f;
};

/// 逐帧描述量。与 `features.py::frame_descriptors()` 同一口径。
inline void frame_descriptors(const std::array<float, kBandCount>& band_dbfs,
                              WindowFrame* out) {
    std::array<float, kBandCount> power{};
    float total = 0.0f;
    float log_sum = 0.0f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        power[band] = std::pow(10.0f, band_dbfs[band] / 10.0f);
        total += power[band];
        log_sum += std::log(std::max(power[band], 1e-20f));
    }
    total = std::max(total, 1e-20f);

    float centroid = 0.0f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        centroid += power[band] * kBandCenterHz[band];
    }
    out->centroid = centroid / total;
    out->flatness = std::exp(log_sum / static_cast<float>(kBandCount))
                    / (total / static_cast<float>(kBandCount));

    // 语音带：300–4000 Hz，按中心频率判断（与 controller.cpp 的 in_speech_band 和
    // features.py 的 VOCAL_BANDS 同一口径：包含 3414.7 Hz、不含 4036.1 Hz）。
    float vocal_sum = 0.0f;
    float vocal_max = 0.0f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        if (kBandCenterHz[band] < 300.0f || kBandCenterHz[band] > 4000.0f) {
            continue;
        }
        vocal_sum += power[band];
        vocal_max = std::max(vocal_max, power[band]);
    }
    out->vocal_ratio = vocal_sum / total;
    out->vocal_peak = vocal_max / std::max(vocal_sum, 1e-20f);

    // 频段序号对 dB 的最小二乘斜率（与 controller.cpp 的 spectral_tilt 同口径）。
    constexpr float kIndexMean = (kBandCount - 1) / 2.0f;
    float numerator = 0.0f;
    float denominator = 0.0f;
    float level_mean = 0.0f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        level_mean += band_dbfs[band];
    }
    level_mean /= static_cast<float>(kBandCount);
    for (std::size_t band = 0; band < kBandCount; ++band) {
        const float centered = static_cast<float>(band) - kIndexMean;
        numerator += centered * (band_dbfs[band] - level_mean);
        denominator += centered * centered;
    }
    out->slope = numerator / denominator;

    float low_power = 0.0f;
    for (std::size_t band = 0; band < kLowEnd; ++band) {
        low_power += power[band];
    }
    out->low_share = low_power / total;
    out->band_dbfs = band_dbfs;
}

/// 由"窗 + 基线"算 15 维特征。基线由调用方给（固件里是噪声地板跟踪器），
/// 这样特征口径可以单独做金标准比对，不把跟踪器的历史混进来。
inline Features compute_window_features(const std::array<float, kBandCount>& base,
                                        float base_level,
                                        const std::array<WindowFrame, kWindowFrames>& frames) {
    Features out;
    float d_lo = 0.0f, d_mid = 0.0f, d_hi = 0.0f;
    float cent = 0.0f, flat = 0.0f, vr = 0.0f;
    float level = 0.0f, vocal_peak = 0.0f, slope = 0.0f, low_share = 0.0f;
    std::array<float, kWindowFrames> env_lo{};
    std::array<float, kWindowFrames> env_mid{};

    for (std::size_t frame = 0; frame < kWindowFrames; ++frame) {
        const WindowFrame& item = frames[frame];
        float lo = 0.0f;
        float mid = 0.0f;
        for (std::size_t band = 0; band < kLowEnd; ++band) {
            d_lo += item.band_dbfs[band] - base[band];
            lo += item.band_dbfs[band];
        }
        for (std::size_t band = kLowEnd; band < kMidEnd; ++band) {
            d_mid += item.band_dbfs[band] - base[band];
            mid += item.band_dbfs[band];
        }
        for (std::size_t band = kMidEnd; band < kBandCount; ++band) {
            d_hi += item.band_dbfs[band] - base[band];
        }
        lo /= static_cast<float>(kLowEnd);
        mid /= static_cast<float>(kMidEnd - kLowEnd);
        env_lo[frame] = lo;
        env_mid[frame] = mid;

        cent += item.centroid;
        flat += item.flatness;
        vr += item.vocal_ratio;
        level += item.broadband_dbfs;
        vocal_peak += item.vocal_peak;
        slope += item.slope;
        low_share += item.low_share;
    }

    constexpr float kFrames = static_cast<float>(kWindowFrames);
    d_lo /= kFrames * static_cast<float>(kLowEnd);
    d_mid /= kFrames * static_cast<float>(kMidEnd - kLowEnd);
    d_hi /= kFrames * static_cast<float>(kBandCount - kMidEnd);

    float mean_lo = 0.0f, mean_mid = 0.0f;
    for (std::size_t frame = 0; frame < kWindowFrames; ++frame) {
        mean_lo += env_lo[frame];
        mean_mid += env_mid[frame];
    }
    mean_lo /= kFrames;
    mean_mid /= kFrames;
    float var_lo = 0.0f, var_mid = 0.0f;
    float min_lo = env_lo[0], max_lo = env_lo[0];
    for (std::size_t frame = 0; frame < kWindowFrames; ++frame) {
        var_lo += (env_lo[frame] - mean_lo) * (env_lo[frame] - mean_lo);
        var_mid += (env_mid[frame] - mean_mid) * (env_mid[frame] - mean_mid);
        min_lo = std::min(min_lo, env_lo[frame]);
        max_lo = std::max(max_lo, env_lo[frame]);
    }

    out.d_lo = d_lo;
    out.d_mid = d_mid;
    out.d_hi = d_hi;
    out.mid_lo = d_mid - d_lo;
    out.hi_mid = d_hi - d_mid;
    out.cent = cent / kFrames;
    out.flat = flat / kFrames;
    out.vr = vr / kFrames;
    out.std_lo = std::sqrt(var_lo / kFrames);
    out.range_lo = max_lo - min_lo;
    out.std_mid = std::sqrt(var_mid / kFrames);
    out.level_rel = level / kFrames - base_level;
    out.vocal_peak = vocal_peak / kFrames;
    out.slope = slope / kFrames;
    out.low_share = low_share / kFrames;
    return out;
}

/// 单窗规则。与 `evaluate.py::raw_classify()` 逐条件一致。
inline Class raw_classify(const Features& f, const Config& config = Config{}) {
    if (f.low_share < config.keyboard_low_share &&
        f.std_mid > config.keyboard_std_mid &&
        f.cent > config.keyboard_cent &&
        (f.slope > config.keyboard_slope || f.cent > config.keyboard_cent_escape)) {
        return Class::keyboard;
    }
    if (f.slope < config.wind_slope &&
        f.vocal_peak < config.wind_vocal_peak &&
        f.d_lo > config.wind_d_lo &&
        f.range_lo > config.wind_range_lo) {
        return Class::wind;
    }
    if (f.vocal_peak > config.speech_vocal_peak) {
        return Class::speech;
    }
    return Class::quiet;
}

/// 状态机：把逐窗判断变成带保持的事件状态。与 `evaluate.py::smooth()` 一致。
class StateMachine {
public:
    void reset() {
        state_ = Class::quiet;
        hold_until_s_ = -1.0f;
    }

    void set_config(const Config& config) { config_ = config; }

    /// now_s：单调时钟（秒）。返回值同时更新内部状态。
    Class update(Class raw, float now_s) {
        switch (raw) {
            case Class::keyboard:
                state_ = Class::keyboard;
                hold_until_s_ = now_s + config_.hold_keyboard_s;
                break;
            case Class::wind:
                if (!(state_ == Class::keyboard && now_s <= hold_until_s_)) {
                    state_ = Class::wind;
                    hold_until_s_ = now_s + config_.hold_wind_s;
                }
                break;
            case Class::speech:
                if (now_s > hold_until_s_) {
                    state_ = Class::speech;
                    hold_until_s_ = now_s + config_.hold_speech_s;
                }
                break;
            case Class::quiet:
                if (now_s > hold_until_s_) {
                    state_ = Class::quiet;
                }
                break;
            case Class::artifact:
                state_ = Class::artifact;
                hold_until_s_ = now_s + config_.hold_artifact_s;
                break;
        }
        return state_;
    }

    Class state() const { return state_; }

private:
    Config config_{};
    Class state_ = Class::quiet;
    float hold_until_s_ = -1.0f;
};

/// 噪声地板跟踪器。向下快、向上慢：说话/风/键盘不会把地板抬走。
class FloorTracker {
public:
    void reset() {
        initialized_ = false;
        count_ = 0;
        base_.fill(-120.0f);
        level_ = -120.0f;
    }

    void set_config(const Config& config) { config_ = config; }

    /// 只接受有效帧；无效帧（削波）不更新地板。
    void push(const std::array<float, kBandCount>& band_dbfs, float broadband_dbfs,
              bool mic_valid) {
        if (!mic_valid) {
            return;
        }
        if (!initialized_) {
            init_frames_[count_] = band_dbfs;
            init_levels_[count_] = broadband_dbfs;
            ++count_;
            if (count_ == kWindowFrames) {
                // 与 features.py 一致：10 帧逐频段取中位数（偶数个取中间两个的平均）。
                for (std::size_t band = 0; band < kBandCount; ++band) {
                    std::array<float, kWindowFrames> values{};
                    for (std::size_t i = 0; i < kWindowFrames; ++i) {
                        values[i] = init_frames_[i][band];
                    }
                    std::sort(values.begin(), values.end());
                    base_[band] = 0.5f * (values[kWindowFrames / 2 - 1] + values[kWindowFrames / 2]);
                }
                std::array<float, kWindowFrames> levels = init_levels_;
                std::sort(levels.begin(), levels.end());
                level_ = 0.5f * (levels[kWindowFrames / 2 - 1] + levels[kWindowFrames / 2]);
                initialized_ = true;
            }
            return;
        }
        for (std::size_t band = 0; band < kBandCount; ++band) {
            const float delta = band_dbfs[band] - base_[band];
            const float rate = delta < 0.0f ? config_.floor_down : config_.floor_up;
            base_[band] += rate * delta;
        }
        const float delta = broadband_dbfs - level_;
        level_ += (delta < 0.0f ? config_.floor_down : config_.floor_up) * delta;
    }

    bool ready() const { return initialized_; }
    const std::array<float, kBandCount>& base() const { return base_; }
    float level() const { return level_; }

private:
    Config config_{};
    bool initialized_ = false;
    std::size_t count_ = 0;
    std::array<float, kBandCount> base_{};
    std::array<std::array<float, kBandCount>, kWindowFrames> init_frames_{};
    std::array<float, kWindowFrames> init_levels_{};
    float level_ = -120.0f;
};

/// 输出：给遥测用。`valid=false` 表示窗还没攒满（或刚被撞击清空）。
struct Output {
    Class cls = Class::quiet;
    bool valid = false;
    Features features{};
};

/// 端侧事件判断器。对象约 1.6 KB（10 帧 × 24 频段 + 描述量），放堆上、不要放任务栈。
class Detector {
public:
    void reset() {
        floor_.reset();
        state_.reset();
        count_ = 0;
        next_ = 0;
    }

    void set_config(const Config& config) {
        config_ = config;
        floor_.set_config(config);
        state_.set_config(config);
    }

    /// 推入一帧（10 Hz）。`now_s` 是单调秒；窗满之后每帧都会更新状态机。
    Output push(const std::array<float, kBandCount>& band_dbfs, float broadband_dbfs,
                bool mic_valid, float now_s) {
        Output out;
        if (!mic_valid) {
            // 撞击/削波：清窗、报 artifact。绝不把这一帧喂给事件窗——瞬态撞击的
            // 波形和键盘咔哒同类，混进窗里就会把它判成键盘。
            count_ = 0;
            next_ = 0;
            out.cls = state_.update(Class::artifact, now_s);
            out.valid = false;
            return out;
        }

        floor_.push(band_dbfs, broadband_dbfs, mic_valid);
        WindowFrame frame;
        frame.broadband_dbfs = broadband_dbfs;
        frame_descriptors(band_dbfs, &frame);
        frames_[next_] = frame;
        next_ = (next_ + 1) % kWindowFrames;
        if (count_ < kWindowFrames) {
            ++count_;
        }
        if (count_ < kWindowFrames || !floor_.ready()) {
            out.valid = false;
            out.cls = Class::quiet;
            return out;
        }

        // 把环形缓冲按时间顺序摊平。10 帧的复制比每次算下标便宜，也少一类绕回 bug。
        std::array<WindowFrame, kWindowFrames> ordered{};
        for (std::size_t i = 0; i < kWindowFrames; ++i) {
            ordered[i] = frames_[(next_ + i) % kWindowFrames];
        }
        out.features = compute_window_features(floor_.base(), floor_.level(), ordered);
        out.cls = state_.update(raw_classify(out.features, config_), now_s);
        out.valid = true;
        return out;
    }

private:
    Config config_{};
    FloorTracker floor_{};
    StateMachine state_{};
    std::array<WindowFrame, kWindowFrames> frames_{};
    std::size_t count_ = 0;
    std::size_t next_ = 0;
};

}  // namespace sound_event
