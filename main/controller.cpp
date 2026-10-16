#include "controller.hpp"

#include <algorithm>
#include <cmath>

namespace {
constexpr float kSampleRate = 48000.0f;
constexpr std::array<float, kBandCount> kBandCenter = {
    70.7f, 141.4f, 244.9f, 346.4f, 451.7f, 566.8f, 696.5f, 841.7f,
    996.8f, 1171.3f, 1370.0f, 1595.0f, 1854.7f, 2154.1f, 2503.3f,
    2916.3f, 3414.7f, 4036.1f, 4829.1f, 5824.1f, 7020.0f, 8552.8f,
    10677.1f, 13638.2f,
};

float clamp01(float value) { return std::clamp(value, 0.0f, 1.0f); }

bool in_speech_band(float hz) { return hz >= 300.0f && hz <= 4000.0f; }

/// Voice-decision thresholds. Grouped and named so they can be re-calibrated against
/// real recordings without touching the DSP code.
namespace voice_thresholds {
/// Absolute speech-presence floor. Below this a frame is treated as silence rather than
/// voice. An earlier revision used -58 dBFS and rejected usable voice at -45 dBFS, which
/// is roughly 10 dB above a real room floor; a target speaker at that level is perfectly
/// audible, so gating there was simply wrong.
constexpr float kGateFloorDbfs = -72.0f;
/// Level gate reaches full scale at this level.
constexpr float kGateFullDbfs = -50.0f;
/// White noise has a near-flat band spectrum: measured flatness 0.693 on the calibration
/// corpus. Speech is far peakier around its formants: measured 0.010-0.012. A ceiling well
/// above the speech range and well below the noise range separates them with a large
/// margin, and this gate is what rejects white noise.
///
/// Why flatness rather than spectral tilt: an earlier revision gated on the least-squares
/// slope of band level against band index. That quantity is an artifact of harmonic
/// spacing. For a 120 Hz voice the low bands hold dozens of harmonics while the high bands
/// hold one, so the measured slope is dominated by how many harmonics land in each band
/// rather than by the spectral envelope. The gate ended up rejecting speech and passing
/// noise, which is exactly backwards. Flatness has no such failure mode.
constexpr float kNoiseFlatnessCeiling = 0.45f;
/// A steady tone concentrates its vocal-band power in one or two bands; speech spreads it
/// across formants. Requiring a low peak share rejects steady tones.
constexpr float kMaxVocalPeakiness = 0.72f;
/// Below this share of power in the speech band there is simply not enough to judge.
constexpr float kMinVocalRatio = 0.34f;
/// Width of the band-ratio ramp above kMinVocalRatio.
constexpr float kVocalRatioSpan = 0.36f;
/// A band at or below this level is treated as unmeasurable and does not count as excited.
constexpr float kFloorDbfs = -95.0f;
/// Speech excites several formant regions at once; a tone excites one. A steady tone that
/// falls between two band centres leaks into both and can score a modest peakiness, so the
/// excited-band count is what actually rejects it.
constexpr std::size_t kMinExcitedVocalBands = 5;
/// Width of the occupancy ramp below kMinExcitedVocalBands.
constexpr float kExcitedBandSoftness = 2.0f;
/// Softness of the flatness and peakiness gates. Smaller is a harder edge.
constexpr float kFlatnessSoftness = 0.10f;
constexpr float kPeakSoftness = 0.08f;

// ── Temporal gate ────────────────────────────────────────────────────────────────
//
// Everything above judges ONE frame. That is not enough, and the failure is measured:
// a hum at f0 ~200 Hz carries ~20 harmonics spaced 200 Hz apart, which land in 8-11 of
// the 24 bands. Peakiness then sits at 0.54-0.63, under kMaxVocalPeakiness (0.72), so the
// gate that exists to reject steady tones never fires and the product reaches 1.00.
// Sweeping 648 physically plausible hums, 47 crossed the 0.55 speech cut, the worst at
// 1.0000 (reproduce with tools/voice_decision_sweep.cpp).
//
// Speech and hum differ in the TIME domain, not the frame. Measured per frame over 6 s:
//
//     speech                level step 5.44 dB   vocal-ratio step 0.107
//     hum f0=200 roll=1.0   level step 0.13 dB   vocal-ratio step 0.003   (the leak)
//     breath                (already rejected by the flatness gate)
//
// **The statistic is the MEDIAN OF SUCCESSIVE ABSOLUTE DIFFERENCES, not the variance.**
// This was measured, not assumed: using the standard deviation over the window, a hum that
// arrives after silence keeps the gate open for a whole window, because the single
// silence-to-sound jump dominates the variance. Measured on 2 s of silence followed by 4 s
// of hum: 14 frames of a 92-frame segment stayed above the 0.55 cut. The median ignores
// that one jump (0.12 dB for silence-then-hum against 1.97 dB for silence-then-speech), so
// the gate reads the *current sound* rather than the transition into it.
//
// The gate takes whichever descriptor moves, which keeps monotone speech (steady level,
// moving formants) passing.
/// Median frame-to-frame level change at which the gate starts to open.
constexpr float kMinLevelStepDb = 0.8f;
/// Ramp width above kMinLevelStepDb.
constexpr float kLevelStepSpanDb = 1.2f;
/// Median frame-to-frame vocal-ratio change at which the gate starts to open.
constexpr float kMinRatioStep = 0.02f;
constexpr float kRatioStepSpan = 0.04f;

}  // namespace voice_thresholds

/// Ramps 0 -> 1 as value rises from `edge` to `edge + softness`.
float soft_above(float value, float edge, float softness) {
    return clamp01((value - edge) / std::max(softness, 1.0e-6f));
}

/// Ramps 1 -> 0 as value rises from `edge` to `edge + softness`.
float soft_below(float value, float edge, float softness) {
    return clamp01((edge - value) / std::max(softness, 1.0e-6f));
}

/// Clipping detection.
///
/// Counting isolated full-scale samples is not enough: at -1 dBFS even clean noise
/// reaches full scale on roughly 26% of samples. What distinguishes a clipped frame is a
/// *plateau* — consecutive samples pinned at full scale. On the calibration corpus, clean
/// content tops out at a 6-sample run, a 1.2x overload reaches 9, and a 3x overload
/// reaches 19, so the longest run is the discriminating signal. The pinned-share test
/// additionally catches square-ish waveforms.
namespace clip_thresholds {
constexpr float kPinnedValue = 32760.0f;
constexpr std::size_t kMaxPinnedRun = 12;
constexpr float kMaxPinnedShare = 0.20f;
}  // namespace clip_thresholds

/// True when the frame looks clipped rather than merely loud.
bool frame_is_clipped(std::size_t pinned_samples, std::size_t longest_pinned_run,
                      std::size_t count) {
    if (count == 0) return false;
    const float pinned_share = static_cast<float>(pinned_samples) / static_cast<float>(count);
    return longest_pinned_run > clip_thresholds::kMaxPinnedRun ||
           pinned_share > clip_thresholds::kMaxPinnedShare;
}

float slew(float current, float target, float dt_ms, float attack_ms, float release_ms) {
    const float tau = target > current ? attack_ms : release_ms;
    const float alpha = 1.0f - std::exp(-std::max(dt_ms, 0.0f) / std::max(tau, 1.0f));
    return current + (target - current) * alpha;
}

/// Hann window for the analysis frame, computed once.
///
/// The window does not depend on the band frequency, so computing it inside the per-band
/// loop meant 24 x 2048 = 49152 transcendentals per frame. That is what made the analysis
/// take 57 ms on device, against a per-frame floating-point budget of roughly 0.2 MFLOP:
/// the arithmetic was never the problem, the window was. Measured on the S3, moving the
/// window out of the loop is the difference between the analysis dominating the control
/// period and being a small part of it.
///
/// Built on first use rather than at namespace scope: the table is 8 KB and only the
/// analysis task touches it, so there is no reason to force it into .data at boot.
const float* hann_window(std::size_t count) {
    static std::array<float, kMaxAnalysisFrame> table{};
    static std::size_t built_for = 0;
    static bool built = false;
    if (!built || built_for != count) {
        const float denominator = static_cast<float>(std::max<std::size_t>(count - 1, 1));
        for (std::size_t i = 0; i < count && i < kMaxAnalysisFrame; ++i) {
            table[i] = 0.5f - 0.5f * std::cos(2.0f * static_cast<float>(M_PI) *
                                              static_cast<float>(i) / denominator);
        }
        built_for = count;
        built = true;
    }
    return table.data();
}

float goertzel_dbfs(const int16_t* samples, std::size_t count, float frequency) {
    const float omega = 2.0f * static_cast<float>(M_PI) * frequency / kSampleRate;
    const float coeff = 2.0f * std::cos(omega);
    const float* window = hann_window(count);
    float s1 = 0.0f;
    float s2 = 0.0f;
    for (std::size_t i = 0; i < count; ++i) {
        const float x = (static_cast<float>(samples[i]) / 32768.0f) * window[i];
        const float s0 = x + coeff * s1 - s2;
        s2 = s1;
        s1 = s0;
    }
    const float power = std::max(s1 * s1 + s2 * s2 - coeff * s1 * s2, 1.0e-12f);
    const float amplitude = 2.0f * std::sqrt(power) / std::max<float>(count, 1.0f);
    return 20.0f * std::log10(std::max(amplitude, 1.0e-6f));
}
}  // namespace

void NoiseAnalyzer::reset() {
    history_count_ = 0;
}


float NoiseAnalyzer::modulation_term() const {
    using namespace voice_thresholds;
    if (history_count_ < kMinHistoryFrames) return 1.0f;

    const std::size_t n = history_count_;
    // Walk the ring oldest -> newest so these are real successive differences.
    const std::size_t oldest = (history_next_ + kHistoryFrames - n) % kHistoryFrames;
    std::array<float, kHistoryFrames> level_steps{};
    std::array<float, kHistoryFrames> ratio_steps{};
    std::size_t steps = 0;
    for (std::size_t i = 1; i < n; ++i) {
        const std::size_t a = (oldest + i - 1) % kHistoryFrames;
        const std::size_t b = (oldest + i) % kHistoryFrames;
        level_steps[steps] = std::fabs(level_history_[b] - level_history_[a]);
        ratio_steps[steps] = std::fabs(ratio_history_[b] - ratio_history_[a]);
        ++steps;
    }
    if (steps == 0) return 1.0f;

    // Median, not variance: see the note on kMinLevelStepDb — one silence-to-sound jump
    // would otherwise hold the gate open across a whole window.
    const auto median = [&steps](std::array<float, kHistoryFrames>& v) {
        std::sort(v.begin(), v.begin() + static_cast<std::ptrdiff_t>(steps));
        return v[steps / 2];
    };
    const float level_step = median(level_steps);
    const float ratio_step = median(ratio_steps);

    // Whichever descriptor shows movement opens the gate. Using the max rather than the
    // product keeps monotone speech (steady level, moving formants) passing, and it cannot
    // open the gate for a sound that is steady in both.
    const float by_level = clamp01((level_step - kMinLevelStepDb) / kLevelStepSpanDb);
    const float by_ratio = clamp01((ratio_step - kMinRatioStep) / kRatioStepSpan);
    return std::max(by_level, by_ratio);
}

AnalysisFrame NoiseAnalyzer::analyze(const int16_t* mono, std::size_t count,
                                     uint32_t timestamp_ms, float motion) {
    AnalysisFrame frame;
    frame.timestamp_ms = timestamp_ms;
    frame.motion = motion;
    frame.band_dbfs.fill(-120.0f);
    if (mono == nullptr || count < 256) return frame;

    double sum = 0.0;
    std::size_t pinned = 0;
    std::size_t longest_run = 0;
    std::size_t current_run = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const float x = static_cast<float>(mono[i]) / 32768.0f;
        sum += x * x;
        if (std::abs(mono[i]) >= clip_thresholds::kPinnedValue) {
            ++pinned;
            ++current_run;
            longest_run = std::max(longest_run, current_run);
        } else {
            current_run = 0;
        }
    }
    const float rms = std::sqrt(static_cast<float>(sum / count));
    frame.broadband_dbfs = 20.0f * std::log10(std::max(rms, 1.0e-6f));
    frame.mic_valid = std::isfinite(frame.broadband_dbfs) &&
                      !frame_is_clipped(pinned, longest_run, count);

    for (std::size_t band = 0; band < kBandCount; ++band) {
        frame.band_center_hz[band] = kBandCenter[band];
        frame.band_dbfs[band] = goertzel_dbfs(mono, count, kBandCenter[band]);
    }
    const VoiceFeatures features = extract_voice_features(frame);
    frame.voice_likelihood = voice_likelihood_from_features(features);

    // Temporal gate. The current frame is pushed first so it participates in its own
    // window; with kHistoryFrames = 15 a single frame cannot move the variance much.
    level_history_[history_next_] = frame.broadband_dbfs;
    ratio_history_[history_next_] = features.vocal_ratio;
    history_next_ = (history_next_ + 1) % kHistoryFrames;
    if (history_count_ < kHistoryFrames) ++history_count_;
    frame.voice_likelihood *= modulation_term();


    return frame;
}

VoiceFeatures extract_voice_features(const AnalysisFrame& frame) {
    VoiceFeatures features;

    std::array<float, kBandCount> power{};
    float total_power = 0.0f;
    float vocal_power = 0.0f;
    float vocal_peak = 0.0f;
    std::size_t excited_vocal_bands = 0;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        power[band] = std::pow(10.0f, frame.band_dbfs[band] / 10.0f);
        total_power += power[band];
        if (in_speech_band(frame.band_center_hz[band])) {
            vocal_power += power[band];
            vocal_peak = std::max(vocal_peak, power[band]);
            if (frame.band_dbfs[band] > voice_thresholds::kFloorDbfs) ++excited_vocal_bands;
        }
    }

    features.vocal_ratio = vocal_power / std::max(total_power, 1.0e-12f);
    features.vocal_peakiness = vocal_peak / std::max(vocal_power, 1.0e-12f);
    features.excited_vocal_bands = excited_vocal_bands;

    // Least-squares slope of band level against band index, fitted over measurable bands
    // only. Natural speech rolls off towards high frequencies; a flat spectrum gives a
    // slope near zero. Floor-limited bands must be excluded or a narrowband signal
    // manufactures a steep slope out of its own silence.
    float mean_index = 0.0f;
    float mean_level = 0.0f;
    std::size_t fitted = 0;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        if (frame.band_dbfs[band] <= voice_thresholds::kFloorDbfs) continue;
        mean_index += static_cast<float>(band);
        mean_level += frame.band_dbfs[band];
        ++fitted;
    }
    if (fitted >= 2) {
        mean_index /= static_cast<float>(fitted);
        mean_level /= static_cast<float>(fitted);
        float covariance = 0.0f;
        float variance = 0.0f;
        for (std::size_t band = 0; band < kBandCount; ++band) {
            if (frame.band_dbfs[band] <= voice_thresholds::kFloorDbfs) continue;
            const float dx = static_cast<float>(band) - mean_index;
            covariance += dx * (frame.band_dbfs[band] - mean_level);
            variance += dx * dx;
        }
        features.spectral_tilt_db_per_band = covariance / std::max(variance, 1.0e-6f);
    }

    double log_sum = 0.0;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        log_sum += std::log(std::max(power[band], 1.0e-20f));
    }
    const float geometric_mean = std::exp(static_cast<float>(log_sum / kBandCount));
    const float arithmetic_mean = std::max(total_power / kBandCount, 1.0e-20f);
    features.spectral_flatness = clamp01(geometric_mean / arithmetic_mean);

    features.level_gate = clamp01((frame.broadband_dbfs - voice_thresholds::kGateFloorDbfs) /
                                  (voice_thresholds::kGateFullDbfs -
                                   voice_thresholds::kGateFloorDbfs));
    return features;
}

float voice_likelihood_from_features(const VoiceFeatures& features) {
    using namespace voice_thresholds;

    // Energy must actually be in the speech band.
    const float band_term = clamp01((features.vocal_ratio - kMinVocalRatio) / kVocalRatioSpan);
    // ... a single dominant band is a tone, not voice.
    const float peak_term = soft_below(features.vocal_peakiness, kMaxVocalPeakiness,
                                      kPeakSoftness);
    // ... a flat spectrum is noise, not voice.
    const float noise_term = soft_below(features.spectral_flatness, kNoiseFlatnessCeiling,
                                       kFlatnessSoftness);
    // ... and voice excites several formant regions, not one.
    const float spread_term =
        soft_above(static_cast<float>(features.excited_vocal_bands),
                   static_cast<float>(kMinExcitedVocalBands) - kExcitedBandSoftness,
                   kExcitedBandSoftness);

    return band_term * peak_term * noise_term * spread_term * features.level_gate;
}

/// 今天的"自身语音"证据近似。
///
/// 设备上还没有声纹/骨传导检测器（voiceprint_gate 只有接口，主工程没接模型；IMU 也没进主工程），
/// 所以这里用"近场电平优势 x 语音判据"作为可标定的占位。**它不是 Apple 的判据**：Apple 用的是
/// 内耳麦克风 + 加速度计的骨传导证据，因此这份代理已知会把"附近有人大声说话"误当成自己开口。
/// 接入真正的自身语音检测器时只替换这个函数，conversation::Awareness 与其全部时序参数都不动。
float own_voice_proxy(const AnalysisFrame& frame, float voice) {
    constexpr float kNearFieldLowDb = -45.0f;
    constexpr float kNearFieldHighDb = -25.0f;
    const float level = clamp01((frame.broadband_dbfs - kNearFieldLowDb) /
                                (kNearFieldHighDb - kNearFieldLowDb));
    return clamp01(voice) * level;
}

EarbudCommand AdaptController::update(const AnalysisFrame& frame, float dt_ms) {
    EarbudCommand command;
    command.sequence = ++sequence_;
    command.timestamp_ms = frame.timestamp_ms;
    command.eq_gain_db.fill(0.0f);
    command.ambient_band_dbfs = frame.band_dbfs;

    if (!frame.mic_valid || !std::isfinite(frame.broadband_dbfs)) {
        invalid_ms_ += dt_ms;
        valid_ms_ = 0.0f;
        speech_gain_db_ = slew(speech_gain_db_, 0.0f, dt_ms, 100.0f, 250.0f);
        media_percent_ = slew(media_percent_, 100.0f, dt_ms, 100.0f, 250.0f);
        // 麦克风不可信时不能留着下压：状态机整体复位，媒体由上面的 slew 带回 100%。
        awareness_.reset();
        command.state = invalid_ms_ >= 250.0f ? AdaptState::mic_fault : AdaptState::recovering;
    } else {
        invalid_ms_ = 0.0f;
        valid_ms_ += dt_ms;
        const float noise = clamp01((frame.broadband_dbfs + 58.0f) / 30.0f);
        const float voice = clamp01(frame.voice_likelihood);
        const bool speech_active = voice > 0.55f && frame.broadband_dbfs > -58.0f;

        // 触发交给 Apple 式对话感知：判据是"佩戴者自己开口"，不是"附近有人在说话"。
        // 旧轮次启发式（语音→300-2500 ms 停顿→再语音，或 2.4 s 持续语音）已删除：它检的是
        // "这段音频像不像对话"，播客/电视/路人说话都能满足，方向就是错的。
        conversation::Evidence evidence;
        evidence.speech_active = speech_active;
        evidence.voice_likelihood = voice;
        evidence.level_dbfs = frame.broadband_dbfs;
        evidence.own_voice = own_voice_proxy(frame, voice);
        const conversation::Output awareness_out = awareness_.update(evidence, dt_ms);

        command.conversation = awareness_out.engaged
                                   ? ConversationState::likely
                                   : (speech_active ? ConversationState::speech_nearby
                                                    : ConversationState::none);

        // The actions are deliberately distinct:
        //   * SONG_EQ asks the Mac renderer to adapt one of the three local songs.
        //   * EXTERNAL_VOICE_INTENT asks for real-world speech gain. AirPods Pro cannot execute
        //     that command through a public API, so the Mac bridge must report it as simulated
        //     until the S3-mic relay path is measured or an open earbud DSP replaces AirPods.
        //   * SAFETY_HOLD forbids positive gain in a very loud non-speech environment. It is a
        //     conservative proxy, not a car-sound classifier; crossing-road recognition remains
        //     a later model/calibration task.
        if (frame.broadband_dbfs < -56.0f) {
            command.environment = EnvironmentKind::quiet;
            command.tuning_mode = TuningMode::flat;
        } else if (speech_active || command.conversation == ConversationState::likely) {
            command.environment = EnvironmentKind::voice_likely;
            command.tuning_mode = TuningMode::external_voice_intent;
        } else if (frame.broadband_dbfs > -30.0f) {
            command.environment = EnvironmentKind::loud;
            command.tuning_mode = TuningMode::safety_hold;
        } else {
            command.environment = EnvironmentKind::steady_noise;
            command.tuning_mode = TuningMode::song_eq;
        }

        // Exaggeration is deliberate and it is not applied uniformly:
        //   * `voice` (how speech-like the frame is) drives the swing, so the command still tracks
        //     the input rather than sitting at a constant maximum.
        //   * `noise` sets the floor, so a quiet room does not get 8 dB of gain for nothing.
        // The result spans roughly 1 dB (barely speech-like) to 8 dB (clear speech in noise),
        // which is a visible and audible swing. The product policy (kMaxSpeechGainDb) is left
        // intact above; this is the demo's ceiling.
        float voice_target = 0.0f;
        float media_target = 100.0f;
        if (command.tuning_mode == TuningMode::external_voice_intent) {
            const float drive = clamp01(0.25f + 1.35f * voice);
            voice_target = std::min(kDemoSpeechGainDb, noise * kDemoSpeechGainDb * drive + 0.4f);
            // Ducking is what makes the lift noticeable at all: raising a band while the bed stays
            // put is easy to miss, pulling the bed down is not.
            //
            // Depth is driven by `speech_run_ms_` -- how long speech has been continuously
            // detected -- not by the per-frame `voice` value. Per-frame confidence oscillates, and
            // gating the duck on it produced a target that wandered between 40% and 100% frame to
            // frame, so the slew never settled and the media only reached 46% instead of the 25%
            // the policy asks for. Sustained speech is also the honest trigger: a one-frame blip
            // should not slam the bed to the floor.
            // 深度斜坡：从"刚听到人声"到"压到底"用 250 ms。原来是 600 ms，叠加 slew 的
            // 时间常数后实测要 700 ms 才压到 30%——用户的原话是"反应略慢"。
            // 下压深度由状态机给出（它自己负责 onset/duck/hold/restore 全程）。这里不再用
            // "持续语音时长"算第二遍深度：两处各算一次，斜坡就会互相打架 —— 项目里已经有过
            // 一次这样的记录（每秒级抖动让媒体只压到 46%）。
            media_target = awareness_out.media_percent;
        } else if (command.tuning_mode == TuningMode::safety_hold) {
            // Never add gain in a loud non-speech environment.
            voice_target = 0.0f;
        }
        // 对话感知一旦成立，媒体就下压，与选哪种调音模式无关：Apple 这条功能不挑模式，
        // 而且"我在说话"本身不依赖环境判据。旧实现把下压挂在 external_voice_intent 分支里，
        // 结果是安静环境下说话（环境判据 quiet → flat）根本不会下压。
        media_target = awareness_out.media_percent;
        const float speech_target = voice_target;
        // Ramps are chosen to be seen and heard. `slew` uses attack_ms when the value rises and
        // release_ms when it falls, so the media ducking -- a fall -- was running on the 800 ms
        // release constant and only reached 36% after 1.8 s of continuous speech. The sibling OWS
        // demo's Beat 1 ramps 100 ms down / 500 ms up, and a demo that takes two seconds to
        // respond reads as "it did nothing".
        //
        // 2026-10-14 复测（阶跃实验，100 ms 拍）：检测本身只要 1 拍（100 ms），但攻击常数
        // 300 ms 意味着到 90% 要 2.3τ ≈ 690 ms，实测第 7 拍才到 7.2 dB。用户反馈"反应略慢"
        // 说的就是这一段。攻击收到 120 ms（到 90% 约 275 ms），释放保持慢一些（600 ms）——
        // 释放太快会让增益在人声停顿处抖回去，反而更像坏了。
        speech_gain_db_ = slew(speech_gain_db_, speech_target, dt_ms, 120.0f, 600.0f);
        media_percent_ = slew(media_percent_, media_target, dt_ms, 500.0f, 100.0f);

        const bool moving = std::abs(speech_target - speech_gain_db_) > 0.25f ||
                            std::abs(media_target - media_percent_) > 2.0f;
        command.state = valid_ms_ < 500.0f ? AdaptState::observing
                                           : (moving ? AdaptState::adjusting : AdaptState::holding);
        command.confidence = clamp01(valid_ms_ / 1200.0f) * (1.0f - clamp01(frame.motion / 4.0f) * 0.35f);

        for (std::size_t band = 0; band < kBandCount; ++band) {
            const bool speech_band = band >= 8 && band <= 16;
            if (command.tuning_mode == TuningMode::external_voice_intent) {
                // The speech band moves with the scalar, so `eq_gain_db` is a real per-band
                // prescription rather than a flat line. Previously this branch wrote nothing and
                // the 24-band curve stayed at 0 dB while the scalar said 5 dB -- the panel and the
                // device screen disagreed about whether anything was happening.
                //
                // A gentle taper on the outermost speech bands keeps the curve readable as a
                // hump; the demo is meant to be obvious, not to be a rectangle.
                const float edge = (band == 8 || band == 16) ? 0.75f : 1.0f;
                // Tuning, not a broadband volume change: bands outside the speech range stay at
                // 0 dB. Without this guard every one of the 24 bands was lifted, which the
                // controller test caught as "bands outside the speech band stay flat" -- the demo
                // being loud is not the same as the tuning being right.
                command.eq_gain_db[band] = speech_band ? speech_gain_db_ * edge : 0.0f;
            } else if (command.tuning_mode == TuningMode::song_eq) {
                // A bounded environment-only suggestion.  The Mac combines the ambient
                // spectrum sent below with the clean pre-EQ spectrum of one of the three local
                // songs before applying its final content-aware prescription.
                const float band_noise = clamp01((frame.band_dbfs[band] + 62.0f) / 32.0f);
                const float importance = speech_band ? 1.0f : (band < 5 ? 0.45f : 0.70f);
                command.eq_gain_db[band] = std::min(4.0f, 4.0f * band_noise * importance);
            }
        }
    }

    // The ceilings live here, at the boundary, so there is exactly one place to reason about.
    // Both were product-policy values and both silenced the demo: the gain clamp cut the 8 dB
    // target back to 6.5, and the media floor of 65% undid the duck to 25% on the way out. The
    // internal targets and the emitted values have to agree, or the device screen and the host
    // panel report a smaller action than the one the loop decided on.
    command.speech_gain_db = std::clamp(speech_gain_db_, 0.0f, kDemoSpeechGainDb);
    command.external_voice_gain_db = command.speech_gain_db;
    command.media_percent = static_cast<uint8_t>(std::lround(
        std::clamp(media_percent_, static_cast<float>(kDemoMediaFloorPercent), 100.0f)));
    return command;
}

void AdaptController::reset() {
    sequence_ = 0;
    speech_gain_db_ = 0.0f;
    media_percent_ = 100.0f;
    valid_ms_ = 0.0f;
    invalid_ms_ = 0.0f;
    // 下压地板只有一处定义：kDemoMediaFloorPercent。把它推进状态机的参数里，
    // 模块默认值只是同名同值的兜底。
    conversation::Params awareness_params;
    awareness_params.duck_floor_percent = static_cast<float>(kDemoMediaFloorPercent);
    awareness_.set_params(awareness_params);
    awareness_.reset();
}

const char* adapt_state_name(AdaptState state) {
    switch (state) {
        case AdaptState::observing: return "OBSERVING";
        case AdaptState::adjusting: return "ADJUSTING";
        case AdaptState::holding: return "HOLDING";
        case AdaptState::recovering: return "RECOVERING";
        case AdaptState::mic_fault: return "MIC FAULT";
    }
    return "UNKNOWN";
}

const char* environment_name(EnvironmentKind environment) {
    switch (environment) {
        case EnvironmentKind::quiet: return "QUIET";
        case EnvironmentKind::steady_noise: return "NOISY";
        case EnvironmentKind::voice_likely: return "VOICE";
        case EnvironmentKind::loud: return "LOUD";
    }
    return "UNKNOWN";
}

const char* tuning_mode_name(TuningMode mode) {
    switch (mode) {
        case TuningMode::flat: return "FLAT";
        case TuningMode::song_eq: return "SONG_EQ";
        case TuningMode::external_voice_intent: return "EXTERNAL_VOICE_INTENT";
        case TuningMode::safety_hold: return "SAFETY_HOLD";
    }
    return "UNKNOWN";
}

const char* conversation_state_name(ConversationState state) {
    switch (state) {
        case ConversationState::none: return "NONE";
        case ConversationState::speech_nearby: return "SPEECH_NEARBY";
        case ConversationState::likely: return "LIKELY";
    }
    return "UNKNOWN";
}
