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

AnalysisFrame NoiseAnalyzer::analyze(const int16_t* mono, std::size_t count,
                                     uint32_t timestamp_ms, float motion) const {
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
    frame.voice_likelihood = voice_likelihood_from_features(extract_voice_features(frame));
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
        speech_run_ms_ = 0.0f;
        silence_run_ms_ += dt_ms;
        conversation_hold_ms_ = std::max(0.0f, conversation_hold_ms_ - dt_ms);
        previous_speech_active_ = false;
        command.state = invalid_ms_ >= 250.0f ? AdaptState::mic_fault : AdaptState::recovering;
    } else {
        invalid_ms_ = 0.0f;
        valid_ms_ += dt_ms;
        const float noise = clamp01((frame.broadband_dbfs + 58.0f) / 30.0f);
        const float voice = clamp01(frame.voice_likelihood);
        const bool speech_active = voice > 0.55f && frame.broadband_dbfs > -58.0f;

        // A single voice-like frame means only "speech nearby".  It is not evidence that the
        // wearer is in a conversation: a podcast, television or one passing sentence has the
        // same spectrum.  Conversation becomes likely only after turn-shaped activity (speech,
        // a 300-2500 ms pause, then speech again), or after sustained nearby speech.  It is held
        // briefly so a natural pause does not make the UI and tuning flap.
        if (speech_active) {
            if (!previous_speech_active_ && silence_run_ms_ >= 300.0f &&
                silence_run_ms_ <= 2500.0f) {
                speech_turns_ = static_cast<uint8_t>(std::min<int>(speech_turns_ + 1, 3));
            }
            speech_run_ms_ += dt_ms;
            silence_run_ms_ = 0.0f;
            if (speech_turns_ >= 2 || speech_run_ms_ >= 2400.0f) {
                conversation_hold_ms_ = 3500.0f;
            }
        } else {
            silence_run_ms_ += dt_ms;
            speech_run_ms_ = 0.0f;
            conversation_hold_ms_ = std::max(0.0f, conversation_hold_ms_ - dt_ms);
            if (silence_run_ms_ > 5000.0f) speech_turns_ = 0;
        }
        previous_speech_active_ = speech_active;

        if (conversation_hold_ms_ > 0.0f) {
            command.conversation = ConversationState::likely;
        } else if (speech_active) {
            command.conversation = ConversationState::speech_nearby;
        }

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

        const float voice_target = command.tuning_mode == TuningMode::external_voice_intent
            ? std::min(kMaxSpeechGainDb, noise * (2.5f + 4.0f * voice))
            : 0.0f;
        const float media_target = 100.0f;
        const float speech_target = voice_target;
        speech_gain_db_ = slew(speech_gain_db_, speech_target, dt_ms, 350.0f, 1200.0f);
        media_percent_ = slew(media_percent_, media_target, dt_ms, 250.0f, 800.0f);

        const bool moving = std::abs(speech_target - speech_gain_db_) > 0.25f ||
                            std::abs(media_target - media_percent_) > 2.0f;
        command.state = valid_ms_ < 500.0f ? AdaptState::observing
                                           : (moving ? AdaptState::adjusting : AdaptState::holding);
        command.confidence = clamp01(valid_ms_ / 1200.0f) * (1.0f - clamp01(frame.motion / 4.0f) * 0.35f);

        for (std::size_t band = 0; band < kBandCount; ++band) {
            const bool speech_band = band >= 8 && band <= 16;
            if (command.tuning_mode == TuningMode::song_eq) {
                // A bounded environment-only suggestion.  The Mac combines the ambient
                // spectrum sent below with the clean pre-EQ spectrum of one of the three local
                // songs before applying its final content-aware prescription.
                const float band_noise = clamp01((frame.band_dbfs[band] + 62.0f) / 32.0f);
                const float importance = speech_band ? 1.0f : (band < 5 ? 0.45f : 0.70f);
                command.eq_gain_db[band] = std::min(4.0f, 4.0f * band_noise * importance);
            }
        }
    }

    command.speech_gain_db = std::clamp(speech_gain_db_, 0.0f, kMaxSpeechGainDb);
    command.external_voice_gain_db = command.speech_gain_db;
    command.media_percent = static_cast<uint8_t>(std::lround(std::clamp(media_percent_, 65.0f, 100.0f)));
    return command;
}

void AdaptController::reset() {
    sequence_ = 0;
    speech_gain_db_ = 0.0f;
    media_percent_ = 100.0f;
    valid_ms_ = 0.0f;
    invalid_ms_ = 0.0f;
    speech_run_ms_ = 0.0f;
    silence_run_ms_ = 1000.0f;
    conversation_hold_ms_ = 0.0f;
    speech_turns_ = 0;
    previous_speech_active_ = false;
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
