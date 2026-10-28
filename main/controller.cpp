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

// ── 噪声自适应歌曲 EQ（2026-10-21，D38）─────────────────────────────────────────
// 旧公式按"每段噪声能量"抬增益（4 × band_noise × importance × focus），本质是噪声谱的
// 复读机：2026-10-21 现场 90 s 实测，低频隆隆环境下 24 段建议最大只有 0.34 dB（×2.5 后
// 仍 ≤0.9 dB），1–4 kHz 恒 0 —— 用户说的"该凸显的没凸显"就是这条公式的直接后果。
//
// 新公式把三件事分开，各自有物理含义：
//   * 内容先验 C[b]：歌曲里"该凸显"的固定形状。低频 70/141 Hz 补开放式耳机的低频泄漏，
//     1.6–4 kHz 做人声 presence 驼峰，8.5–13.6 kHz 补空气感；346–842 Hz 的"糊区"给负值，
//     因为两首参考曲目在这里都有 20–27% 的能量，而它正是掩蔽人声的东西。
//   * 噪声暴露 N：安静不动作（N=0），越吵动作越大（N=1）。只放大整体幅度，不改形状。
//   * 噪声主导抑制 D[b]：某一段已经被环境噪声占满时，抬它既听不出来又只会更糊，所以按
//     (1 − k[b]·D[b]) 收掉；削减段（C<0 去糊）不受抑制，因为噪声里削糊总是有利的。
// 曲线是**有符号**的：负增益 = 去糊。设备建议口径为 [-4, +4] dB/段，Mac 播放器再乘面板
// 深度（默认 2.5，夹到 [-12, +10]）。形状与参数的来源与数值见
// work/eq-design/design.md（2026-10-21 现场降噪实验稿）。
// 2026-10-22 网友共识校准：社区通勤 EQ 共识把 250 Hz 列为"一多就糊"的高危段，
// 嘈杂环境应切 200–400 Hz（见 work/eq-design/community-eq-consensus.md），
// 所以 244.9 Hz 段从 +1.0 改为 0——去糊区从 346 Hz 起削、245 Hz 不抬。
constexpr std::array<float, kBandCount> kSongEqContentDb = {
    2.5f, 3.0f, 0.0f, -1.8f, -1.8f, -1.4f, -0.6f, -0.5f,
    0.0f, 0.5f, 1.0f, 1.2f, 1.7f, 2.1f, 2.3f, 2.3f,
    1.9f, 1.5f, 1.3f, 1.3f, 1.3f, 2.6f, 2.6f, 1.9f,
};
/// 正增益的"该段被噪声主导"抑制系数。人声最敢抬（0.45），低频其次（0.65），
/// 空气最不该把嘶声抬起来（0.85）；负增益段为 0（去糊不抑制）。
constexpr std::array<float, kBandCount> kSongEqMaskK = {
    0.65f, 0.65f, 0.65f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f,
    0.60f, 0.60f, 0.60f, 0.45f, 0.45f, 0.45f, 0.45f, 0.45f,
    0.45f, 0.45f, 0.60f, 0.60f, 0.60f, 0.85f, 0.85f, 0.85f,
};
/// 设备建议的每段绝对值上限（dB）。负值需要协议/面板支持（D38）。
constexpr float kSongEqDeviceLimitDb = 4.0f;
/// 噪声暴露 N 的映射：-54 dBFS 以下不动作，-34 以上满幅（与模式路由的安静/高噪门槛对齐）。
constexpr float kSongEqExposureQuietDbfs = -54.0f;
constexpr float kSongEqExposureSpanDb = 20.0f;
/// 每段"被噪声主导程度" D 的映射：-75 dBFS 以下视为干净，-35 以上视为被占满。
constexpr float kSongEqBandFloorDbfs = -75.0f;
constexpr float kSongEqBandSpanDb = 40.0f;
/// 外部人声意图（背景/别人说话）在内容曲线上额外加的人声强调上限（dB）。
// 2026-10-22 demo 口径：背景人声的诉求不是"让人声靠前"，而是"盖住它"（用户明确：
// 无关背景说话压根不想听）。做法是把音乐在语音频段抬满（设备单段上限 +4 dB），
// 再给全频段 +1 dB 的整体抬升；Mac 侧默认 2.5× 处方深度 → 语音段可到 +10 dB。
// 这是 demo 口径、不是产品口径：产品上要按"用户想不想参与"分三态（见 D41）。
constexpr float kExternalVoiceEmphasisDb = 4.0f;   // 单段上限，拉爆
constexpr float kMaskingBroadbandDb = 1.0f;        // 全频段薄薄一层，帮助掩蔽
constexpr std::size_t kExternalVoiceFirstBand = 8;  // 1.0 kHz
constexpr std::size_t kExternalVoiceLastBand = 17;  // 4.04 kHz
/// 掩蔽深度按 voice 置信度连续缩放，而不是"过了 0.55 就整条拉满"：
/// 0.55（speech_active 门限）→0，0.75→满幅。结构上保证阈值处目标曲线连续，
/// 微小噪声在 0.55 附近只会让 EQ 动一点点，不会一步 +4 dB。这里**不加时间去抖**，
/// 所以不会给真实背景人声或环境声增加额外延迟；平滑仍由既有的 650/850 ms EQ slew 负责。
constexpr float kExternalMaskVoiceFloor = 0.55f;
constexpr float kExternalMaskVoiceFull = 0.75f;

// ── 噪声掩蔽 + 键盘特殊掩蔽（2026-10-26，D48）───────────────────────────────────
// 背景：用户反馈"基础档遮蔽一般，尤其键盘噪声"。真机定位（8787 host 快照）：键盘来时
// event=keyboard、broadband ≈ -42 dBFS，模式停在 SONG_EQ、掩蔽推量 ≈ 0、内容曲线只动
// ±0.8 dB——键盘当前没有任何"盖住它"的动作。D41 的掩蔽只认背景人声
// （external_voice_intent）；这里把掩蔽补到噪声上，两条形状分开：
//   * 普通噪声（SONG_EQ）：全频段 +0.3、presence 段（1.6–4 kHz）+1.0；
//     深度 -54→0 / -34→1，与 D39 噪声暴露同映射（安静不动作）。
//   * 键盘（事件为 keyboard 且 SONG_EQ）：全频段 +0.5、键盘足迹段（1.0–3.4 kHz；
//     D17 实测质心 932 Hz、中频 +7.1 dB）+1.5；深度 -52→0 / -34→1。
// 2026-10-26 现场第一版把足迹段直接拉满 +4 dB 且从 696.5 Hz 起：反馈"人声完全变了、
// 毛刺感极强"——幅度砍到 1/3、足迹上移到 1 kHz 起（避开歌曲人声基频区），并给掩蔽
// 深度加了独立时间平滑（起音 400 ms / 释放 1200 ms），不跟咔哒尖峰与
// SONG_EQ↔SAFETY_HOLD 模式边界抽动。
// 对话/自身语音 = flat、安全保持 = 只削不抬，都不叠掩蔽（D25 不变）。掩蔽 ≠ 降噪 ≠
// 隔音：只是把音乐盖上去。
constexpr float kNoiseMaskBroadbandDb = 0.3f;
constexpr float kNoiseMaskEmphasisDb = 1.0f;
constexpr std::size_t kNoiseMaskFirstBand = 11;   // 1595.0 Hz
constexpr std::size_t kNoiseMaskLastBand = 17;    // 4036.1 Hz
constexpr float kNoiseMaskFloorDbfs = -54.0f;
constexpr float kNoiseMaskSpanDb = 20.0f;
constexpr float kKeyboardMaskBroadbandDb = 0.5f;
constexpr float kKeyboardMaskEmphasisDb = 1.5f;
constexpr std::size_t kKeyboardFirstBand = 8;     // 996.8 Hz
constexpr std::size_t kKeyboardLastBand = 16;     // 3414.7 Hz
constexpr float kKeyboardMaskFloorDbfs = -52.0f;
constexpr float kKeyboardMaskSpanDb = 18.0f;
/// 掩蔽深度的独立时间平滑：起音 400 ms、释放 1200 ms。
constexpr float kMaskDepthAttackMs = 400.0f;
constexpr float kMaskDepthReleaseMs = 1200.0f;

float clamp01(float value) { return std::clamp(value, 0.0f, 1.0f); }

/// 一条内容曲线：importance × exposure × (1 − 噪声主导抑制)，有符号，夹到设备口径。
std::array<float, kBandCount> song_clarity_curve(const AnalysisFrame& frame, float exposure) {
    std::array<float, kBandCount> gains{};
    for (std::size_t band = 0; band < kBandCount; ++band) {
        const float dominance =
            clamp01((frame.band_dbfs[band] - kSongEqBandFloorDbfs) / kSongEqBandSpanDb);
        const float value =
            kSongEqContentDb[band] * exposure * (1.0f - kSongEqMaskK[band] * dominance);
        gains[band] = std::clamp(value, -kSongEqDeviceLimitDb, kSongEqDeviceLimitDb);
    }
    return gains;
}

NoiseSpectrum classify_noise_spectrum(const AnalysisFrame& frame) {
    if (!frame.mic_valid || frame.broadband_dbfs < -56.0f) return NoiseSpectrum::none;
    auto mean_power = [&](std::size_t first, std::size_t last) {
        float total = 0.0f;
        for (std::size_t i = first; i < last; ++i) {
            total += std::pow(10.0f, frame.band_dbfs[i] / 10.0f);
        }
        return total / static_cast<float>(last - first);
    };
    // 低：70–567 Hz；中：697 Hz–3.4 kHz；高：4–13.6 kHz。
    const std::array<float, 3> power = {
        mean_power(0, 6), mean_power(6, 17), mean_power(17, kBandCount)};
    std::size_t winner = 0;
    if (power[1] > power[winner]) winner = 1;
    if (power[2] > power[winner]) winner = 2;
    const float other = std::max(power[(winner + 1) % 3], power[(winner + 2) % 3]);
    // 不到 1.8 倍就按混合处理，避免相邻频段在门槛附近来回跳。
    if (power[winner] < other * 1.8f) return NoiseSpectrum::mixed;
    return winner == 0 ? NoiseSpectrum::low
                       : (winner == 1 ? NoiseSpectrum::mid : NoiseSpectrum::high);
}

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

EarbudCommand AdaptController::update(const AnalysisFrame& frame, float dt_ms,
                                    bool non_voice_event,
                                    bool external_self_voice,
                                    bool keyboard_event,
                                    bool speech_event,
                                    bool dialogue_veto) {
    EarbudCommand command;
    command.sequence = ++sequence_;
    command.timestamp_ms = frame.timestamp_ms;
    command.eq_gain_db.fill(0.0f);
    command.ambient_band_dbfs = frame.band_dbfs;
    command.noise_spectrum = classify_noise_spectrum(frame);
    // 判据遥测：训练集与误触发排查都要这些量，决策逻辑一行都不用它们。
    command.broadband_dbfs = frame.broadband_dbfs;
    command.voice_likelihood = frame.voice_likelihood;
    // D47 双麦遥测：只搬运、不决策（与判据遥测同纪律）。
    command.mic2_broadband_dbfs = frame.mic2_broadband_dbfs;
    command.mic_diff_db = frame.mic_diff_db;
    command.mic_coherence = frame.mic_coherence;
    command.mic2_valid = frame.mic2_valid;
    {
        const VoiceFeatures telemetry = extract_voice_features(frame);
        command.vocal_ratio = telemetry.vocal_ratio;
        command.spectral_flatness = telemetry.spectral_flatness;
        command.vocal_peakiness = telemetry.vocal_peakiness;
        command.excited_vocal_bands =
            static_cast<uint8_t>(std::min<std::size_t>(telemetry.excited_vocal_bands, 255));
    }

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
        // 模式路由与噪声暴露用平滑后的电平，不用原始帧：2026-10-21 现场实测 broadband 在
        // -30 dBFS 门槛附近被键盘/拍桌的 100 ms 瞬态推着来回穿，环境标签在 NOISY↔LOUD 间
        // 每 100–200 ms 抖一次，SONG_EQ 与 SAFETY_HOLD 跟着切，EQ 曲线听起来就是抽动。
        // 250 ms 的对称一阶平滑滤掉瞬态，又比 650/850 ms 的 EQ 平滑快一档。
        smoothed_dbfs_ = slew(smoothed_dbfs_, frame.broadband_dbfs, dt_ms, 250.0f, 250.0f);
        const float noise = clamp01((frame.broadband_dbfs + 58.0f) / 30.0f);
        const float voice = clamp01(frame.voice_likelihood);
        // 0.55：歌曲 EQ/场景判断用的"有人声"门槛；
        // 0.65：**对话/环境声**的门槛。实测房间噪声的 voice_likelihood 常漂到 0.5–0.6，
        // 若和 EQ 用同一个门槛，键盘/风扇/远处人声都会反复把对话顶起来（2026-10-19 实测
        // 环境声一直开着）。对话是更重的动作，门槛必须更高。
        constexpr float kConversationVoiceThreshold = 0.65f;
        // self 判定（声纹，3 s 内新鲜）视为哼唱/自言自语：不触发人声 EQ 或对话。
        // other / 没有判定都走 D16 的预判逻辑（D28 step3）。
        //
        // 2026-10-26（D49）：人声判定必须过事件层的 speech 类。旧写法只看 4 判据
        // voice + 电平，贴耳时 OWS 耳机漏音把 voice 顶到 0.8+，对话状态机被持续
        // 证据锁进 engaged → EQ 压平、掩蔽中断（真机实测耳边打字 FLAT 占 47%）。
        // 事件层 speech 在漏音/键盘场景 0 帧触发，在真人回放证据帧 1464/1464 全部
        // 触发——门控对真实对话零损伤（回放连续段统计前后一致），代价是首响 +~1 s
        // （事件窗要先攒 1 s 语音）。
        const bool speech_active =
            speech_event && !external_self_voice &&
            voice > 0.55f && frame.broadband_dbfs > -58.0f;
        const bool conversation_voice =
            speech_event && !external_self_voice &&
            voice > kConversationVoiceThreshold && frame.broadband_dbfs > -58.0f;

        // **预判输入**：附近有人开口 = 对话正在开始。这是提前量的唯一来源——"我开口"只能
        // 在事后被检测到，把它当触发输入（旧实现）就永远只能"说过之后才压"，方向与目标相反。
        // 电平门 -58→-42 dBFS：更弱的语音不可信，-42 以上按近场对话计。
        const float cue_level = clamp01((frame.broadband_dbfs + 58.0f) / 16.0f);
        conversation::Evidence evidence;
        evidence.speech_active = conversation_voice;
        evidence.voice_likelihood = voice;
        evidence.level_dbfs = frame.broadband_dbfs;
        // 键盘/风/撞击（non_voice_event）不参与对话证据；普通噪声也必须在 0.65 以上。
        evidence.conversation_cue = conversation_voice ? cue_level : 0.0f;
        // 事件帧在状态机的确认窗口里"中立"（不计入证据累计、不打断、不累计释放计时）。
        // 键盘是现场噪声大头，把它当普通噪声会把真实对话的窗口打碎（2026-10-23 用户拍板）。
        evidence.event_blocked = non_voice_event;
        // 证据来源按**显式策略**决定，而不是"有就用"。三条路的可信度与失败方式不同，
        // 而且遥测必须如实反映用了哪一条（own_voice_source 与 own_voice_policy）。
        OwnVoiceEvidence effective = own_voice_evidence_;
        switch (evidence_policy_) {
            case EvidencePolicy::bone_only:
                // 产品默认 fail-closed：没有骨传导证据就不动作。骨传导判据已被真机数据否定
                // （docs/apple-conversation-awareness.md 第 13 节），所以这条路的现实表现是
                // "功能不动作"——比"用已知会误触发的代理兜底"安全，也好查。
                if (!effective.bone) {
                    const bool imu_ok = effective.imu_ok;
                    effective = OwnVoiceEvidence{};
                    effective.imu_ok = imu_ok;   // IMU 是否存活仍要如实上报
                }
                break;
            case EvidencePolicy::near_field_demo:
                // 演示模式：明知它只知道"有人说话"。所以 bone 必须是 false、proxy 必须是 true，
                // 面板与串口都会显示 near_field —— 演示可以宽松，口径不能脏。
                effective.bone = false;
                effective.proxy = true;
                effective.confidence =
                    conversation::near_field_own_voice(frame.broadband_dbfs, voice);
                effective.correlation = 0.0f;
                effective.vibration_rms = 0.0f;
                effective.motion = false;
                break;
            case EvidencePolicy::disabled:
                effective = OwnVoiceEvidence{};
                effective.imu_ok = own_voice_evidence_.imu_ok;
                break;
        }
        // 用户 2026-10-19 口径：**不能把自己的声音/近场代理当成"对话开始"**。
        // 近场代理只能说明"有人在我附近说话"，分不出是我还是别人；拿它当证据会让
        // 房间噪声/键盘/自己说话都反复把环境声顶开。在真正的自身语音传感器可用之前，
        // own_voice 一律不作为对话证据（连同 bone 的失败判据一起）。
        (void)effective;
        evidence.own_voice = 0.0f;
        // A1/D50：Mac 的迟到否决（`dialog no` TTL 内）。只否决"是不是对话"，噪声/EQ
        // 继续正常工作——目的是把连续电视/播客误顶起来的 engaged 放回 SONG_EQ，
        // 让 D41/D48 的掩蔽继续工作，而不是把 EQ 压平。
        evidence.dialogue_veto = dialogue_veto;
        awareness_out_ = awareness_.update(evidence, dt_ms);
        const conversation::Output& awareness_out = awareness_out_;
        // 对话正在发生（**已确认的预览下压**或确认）都算"进入对话"：EQ 必须保持平直，
        // 音乐让位。注意这里用 `out.anticipating`（连续证据 ≥500 ms 的预览），而不是
        // `state == anticipating`（第一帧证据就进）——2026-10-28 贴耳打字现场：
        // 单帧瞬态会被 1 s 事件窗标成 speech、4 判据 voice 被音乐/敲击顶到 1.0，旧写法
        // 让状态机 1 帧就 anticipating → EQ 在 100–400 ms 内闪平再回 SONG_EQ（124 s 里
        // 34 次），掩蔽被反复打断。预览前 500 ms 媒体本来也没下压，EQ 不应先动。
        const bool conversation_active =
            awareness_out.anticipating || awareness_out.engaged;

        command.conversation =
            awareness_out.engaged
                ? ConversationState::likely
                : (awareness_out.anticipating
                       ? ConversationState::anticipating
                       : (speech_active ? ConversationState::speech_nearby
                                        : ConversationState::none));
        // 环境声开放意图只在对话**确认**后为真：预测阶段先只压媒体，避免一声咳嗽
        // 就让 AirPods 切到通透模式；执行端的开关动作为此要慢半拍是对的。
        command.ambient_open = awareness_out.ambient_open;

        // 遥测：报"实际生效的那一份"证据（骨传导或代理），而不是原始输入。
        command.own_voice = effective;
        command.evidence_policy = evidence_policy_;

        // 动作是分离的，字段也必须分离：
        //   * SONG_EQ 写"内容重要性 × 噪声暴露 × 噪声主导抑制"的有符号 24 段曲线（D38）；
        //   * EXTERNAL_VOICE_INTENT 用同一条内容曲线，再把 1.4–4 kHz 人声段额外强调一点
        //     （D26 的"背景/别人说话也要把 EQ 拉起来"仍然成立，只是从"只抬中频"升级成
        //     "整条内容曲线 + 人声加权"）；
        //   * SAFETY_HOLD 只允许非正增益：保留去糊削减，绝不抬升（安全口径不变）；
        //     现实人声增益本身仍只走标量 `external_voice_gain_db`，不是"用歌曲中频
        //     假装现实人声增强"——D0 的字段分离不变。
        // 已确认的对话（以及 self 判定）走 flat + 下压：这是 D25/D26 的"对话 EQ 全平"，
        // 不在歌曲上叠任何抬升。
        if (conversation_active || external_self_voice) {
            // 自己说话不算对话：环境声不开、外部人声 EQ 不动；模式保持平直。
            command.environment = EnvironmentKind::voice_likely;
            command.tuning_mode = TuningMode::flat;
        } else if (smoothed_dbfs_ < -56.0f) {
            command.environment = EnvironmentKind::quiet;
            command.tuning_mode = TuningMode::flat;
        } else if (speech_active || command.conversation == ConversationState::likely) {
            command.environment = EnvironmentKind::voice_likely;
            command.tuning_mode = TuningMode::external_voice_intent;
        } else if (smoothed_dbfs_ > -30.0f) {
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
            media_target = external_self_voice ? 100.0f : awareness_out.media_percent;
        } else if (command.tuning_mode == TuningMode::safety_hold) {
            voice_target = 0.0f;
        }
        // 对话感知一旦成立，媒体就下压，与选哪种调音模式无关：Apple 这条功能不挑模式，
        // 而且"我在说话"本身不依赖环境判据。旧实现把下压挂在 external_voice_intent 分支里，
        // 结果是安静环境下说话（环境判据 quiet → flat）根本不会下压。
        media_target = external_self_voice ? 100.0f : awareness_out.media_percent;
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

        // 掩蔽深度：只跟 voice 置信度走，不改变"何时进入 external_voice_intent"
        //（触发仍由上面的 speech_active 决定），也不影响 ambient_open / media_percent。
        // 门限 0.55 处深度为 0，所以模式切换本身不会给 EQ 一个台阶；这里不加时间去抖，
        // 真实背景人声的延迟只来自既有的 650/850 ms EQ slew，没有新增。
        // 线性 0→满幅，而不是平方衰减：0.60 仍保留 25% 掩蔽（D41 听感不至于消失），
        // 0.56 这种刚过线的单帧只有 ~1% 深度，不会再把 EQ 顶起来。
        const float mask_voice =
            clamp01((voice - kExternalMaskVoiceFloor) /
                    (kExternalMaskVoiceFull - kExternalMaskVoiceFloor));
        const float mask_depth =
            command.tuning_mode == TuningMode::external_voice_intent
                ? mask_voice
                : 0.0f;

        // 噪声/键盘掩蔽深度（D48）：目标深度每拍算一次，但用独立的慢时间平滑
        // （起音 400 ms / 释放 1200 ms）。第一版直接用 250 ms 平滑电平当深度，
        // 现场反馈"毛刺感极强"——掩蔽跟着敲击节奏与 SONG_EQ↔SAFETY_HOLD 边界抽动。
        // 这里每拍都更新（包括 flat / safety_hold 模式），让深度在对话或极响期间
        // 也能自然衰减，回 SONG_EQ 时不会带着陈旧的高深度。
        const float noise_mask_target =
            clamp01((smoothed_dbfs_ - kNoiseMaskFloorDbfs) / kNoiseMaskSpanDb);
        const float keyboard_mask_target =
            keyboard_event
                ? clamp01((smoothed_dbfs_ - kKeyboardMaskFloorDbfs) / kKeyboardMaskSpanDb)
                : 0.0f;
        noise_mask_depth_ = slew(noise_mask_depth_, noise_mask_target, dt_ms,
                                 kMaskDepthAttackMs, kMaskDepthReleaseMs);
        keyboard_mask_depth_ = slew(keyboard_mask_depth_, keyboard_mask_target, dt_ms,
                                    kMaskDepthAttackMs, kMaskDepthReleaseMs);

        // 曲线只在三种"在放歌"的模式下写：稳态噪声、背景人声、安全保持。
        // 安静与对话/自身语音保持平直（flat 的 eq 目标为 0，由下面的 slew 带回）。
        if (command.tuning_mode == TuningMode::song_eq ||
            command.tuning_mode == TuningMode::external_voice_intent ||
            command.tuning_mode == TuningMode::safety_hold) {
            const float exposure =
                clamp01((smoothed_dbfs_ - kSongEqExposureQuietDbfs) / kSongEqExposureSpanDb);
            std::array<float, kBandCount> curve = song_clarity_curve(frame, exposure);
            if (command.tuning_mode == TuningMode::external_voice_intent) {
                // 掩蔽（demo）：背景有人说话时把音乐盖上去，而不是把人声抬出来。
                // 全频段 +1 dB，语音段 1–4 kHz 再加到单段上限；Mac 再乘处方深度。
                //
                // 2026-10-22 修正：深度由 voice 置信度连续缩放（mask_depth）。
                // 旧写法在 speech_active 的 0.55 门限处直接整条 +1/+4 dB，单帧噪声一过线
                // 就是一个大台阶；现在门限处深度为 0，voice≥0.75 才到满幅。
                for (std::size_t band = 0; band < kBandCount; ++band) {
                    curve[band] += kMaskingBroadbandDb * mask_depth;
                }
                for (std::size_t band = kExternalVoiceFirstBand;
                     band <= kExternalVoiceLastBand; ++band) {
                    curve[band] += kExternalVoiceEmphasisDb * mask_depth;
                }
            } else if (command.tuning_mode == TuningMode::safety_hold) {
                // 安全保持：禁止正增益（D25 第 3 条）。保留负的去糊段——削减不会把
                // 任何东西推得更响，却在噪声里让音乐少糊一点。
                for (float& gain : curve) gain = std::min(gain, 0.0f);
            } else {
                // 稳态噪声（SONG_EQ）：普通噪声掩蔽 + 键盘特殊掩蔽（2026-10-26，D48）。
                // 键盘事件帧必然是非人声（non_voice_event），只会落进这里，不会与
                // D41 的人声掩蔽叠加；两条掩蔽都吃同一个 ±4 dB 设备上限。深度用上面
                // 平滑过的状态，不在这里重算（否则白做时间平滑）。
                const float noise_mask = noise_mask_depth_;
                if (noise_mask > 0.0f) {
                    for (std::size_t band = 0; band < kBandCount; ++band) {
                        curve[band] += kNoiseMaskBroadbandDb * noise_mask;
                    }
                    for (std::size_t band = kNoiseMaskFirstBand;
                         band <= kNoiseMaskLastBand; ++band) {
                        curve[band] += kNoiseMaskEmphasisDb * noise_mask;
                    }
                }
                const float keyboard_mask = keyboard_mask_depth_;
                if (keyboard_mask > 0.0f) {
                    for (std::size_t band = 0; band < kBandCount; ++band) {
                        curve[band] += kKeyboardMaskBroadbandDb * keyboard_mask;
                    }
                    for (std::size_t band = kKeyboardFirstBand;
                         band <= kKeyboardLastBand; ++band) {
                        curve[band] += kKeyboardMaskEmphasisDb * keyboard_mask;
                    }
                }
            }
            for (std::size_t band = 0; band < kBandCount; ++band) {
                command.eq_gain_db[band] =
                    std::clamp(curve[band], -kSongEqDeviceLimitDb, kSongEqDeviceLimitDb);
            }
        }

        // 频谱每拍都会抖，分类边界也可能在 LOW/MIXED/MID 间切换。对每段独立做时间平滑：
        // 上升 650 ms、回落 850 ms，约 1.5–2 s 完成主要变化；对话/自身语音仍由环境声门控
        // 单独即时处理，不靠 EQ 曲线承担“关麦”职责。
        for (std::size_t band = 0; band < kBandCount; ++band) {
            eq_gain_db_[band] = slew(eq_gain_db_[band], command.eq_gain_db[band], dt_ms,
                                     650.0f, 850.0f);
            command.eq_gain_db[band] = eq_gain_db_[band];
        }
    }

    // The ceilings live here, at the boundary, so there is exactly one place to reason about.
    // Both were product-policy values and both silenced the demo: the gain clamp cut the 8 dB
    // target back to 6.5, and a second media floor could undo the chosen duck on the way out. The
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
    eq_gain_db_.fill(0.0f);
    smoothed_dbfs_ = -120.0f;
    valid_ms_ = 0.0f;
    invalid_ms_ = 0.0f;
    // 下压地板只有一处定义：kDemoMediaFloorPercent。把它推进状态机的参数里，
    // 模块默认值只是同名同值的兜底。
    conversation::Params awareness_params;
    awareness_params.duck_floor_percent = static_cast<float>(kDemoMediaFloorPercent);
    awareness_.set_params(awareness_params);
    awareness_.reset();
}

VoiceprintDecision evaluate_voiceprint(bool valid, bool self_verdict, int64_t age_us,
                                      int64_t hold_us) {
    VoiceprintDecision decision;
    // 负的 hold 是配置错误：当成"永不过期"没有意义，直接视为不激活。
    decision.active = valid && hold_us > 0 && age_us >= 0 && age_us <= hold_us;
    decision.self_voice = decision.active && self_verdict;
    return decision;
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

const char* noise_spectrum_name(NoiseSpectrum spectrum) {
    switch (spectrum) {
        case NoiseSpectrum::none: return "NONE";
        case NoiseSpectrum::low: return "LOW";
        case NoiseSpectrum::mid: return "MID";
        case NoiseSpectrum::high: return "HIGH";
        case NoiseSpectrum::mixed: return "MIXED";
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

const char* evidence_policy_name(EvidencePolicy policy) {
    switch (policy) {
        case EvidencePolicy::bone_only: return "bone_only";
        case EvidencePolicy::near_field_demo: return "near_field_demo";
        case EvidencePolicy::disabled: return "disabled";
    }
    return "UNKNOWN";
}

const char* conversation_state_name(ConversationState state) {
    switch (state) {
        case ConversationState::none: return "NONE";
        case ConversationState::speech_nearby: return "SPEECH_NEARBY";
        case ConversationState::likely: return "LIKELY";
        case ConversationState::anticipating: return "ANTICIPATING";
    }
    return "UNKNOWN";
}
