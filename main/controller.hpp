#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "conversation_awareness.hpp"

constexpr std::size_t kBandCount = 24;
/// Largest analysis frame the window table will hold. The audio task uses 2048 samples.
constexpr std::size_t kMaxAnalysisFrame = 4096;
constexpr float kMaxSpeechGainDb = 6.5f;

/// Demo exaggeration. The numbers above are the *product* policy: bounded, smooth, and chosen so
/// a real listener is not startled. This instrument exists to answer a different question -- "is
/// this闭-loop even happening, and how much is it doing?" -- and at 5.4 dB with a flat EQ the
/// answer looked like "nothing". A demo has to be visible.
///
/// The ceilings are inherited from the user's standing rule for this project (no test may exceed
/// +10 dB of gain) and from the measured OWS result in the sibling project (speech band +7.8 dB
/// measured, +9.7 dB peak, hard constraint 10 dB). So:
///   * gain swing 0 → kDemoSpeechGainDb (8.0 dB, inside the measured/verified range, 2 dB margin)
///   * the same value is written into the speech bands of eq_gain_db, so the 24-band curve moves
///     instead of staying flat -- previously the speech-likely branch left eq_gain_db at zero
///   * media ducks to kDemoMediaFloorPercent (25%), which is the ducking depth the sibling demo's
///     Beat 1 shipped with and is plainly audible
constexpr float kDemoSpeechGainDb = 8.0f;
constexpr uint8_t kDemoMediaFloorPercent = 25;

enum class TuningMode : uint8_t {
    flat,
    song_eq,
    external_voice_intent,
    safety_hold,
};

enum class ConversationState : uint8_t {
    none,
    speech_nearby,
    likely,
};

enum class AdaptState : uint8_t {
    observing,
    adjusting,
    holding,
    recovering,
    mic_fault,
};

enum class EnvironmentKind : uint8_t {
    quiet,
    steady_noise,
    voice_likely,
    loud,
};

struct AnalysisFrame {
    uint32_t timestamp_ms = 0;
    float broadband_dbfs = -120.0f;
    float voice_likelihood = 0.0f;
    float motion = 0.0f;
    std::array<float, kBandCount> band_dbfs{};
    /// Center frequency of each band in Hz, same order as band_dbfs.
    std::array<float, kBandCount> band_center_hz{};
    bool mic_valid = false;
};

/// Measured descriptors of one analysis frame, exposed so the voice decision can be
/// inspected, unit-tested and re-calibrated without touching the DSP.
struct VoiceFeatures {
    /// Share of total band power falling inside the speech band (300-4000 Hz).
    float vocal_ratio = 0.0f;
    /// Least-squares slope of band level versus band index, in dB per band.
    /// Natural speech falls off towards high frequencies, so this is negative.
    float spectral_tilt_db_per_band = 0.0f;
    /// Largest single vocal-band share of the vocal-band power. Near 1.0 for a pure tone.
    float vocal_peakiness = 0.0f;
    /// Number of speech-band bands above the measurement floor. Speech excites several
    /// formant regions at once; a steady tone excites one.
    std::size_t excited_vocal_bands = 0;
    /// Geometric/arithmetic mean ratio of the band power spectrum. Near 1.0 for white noise.
    float spectral_flatness = 0.0f;
    /// Level gate in [0, 1]; low for frames too quiet to judge.
    float level_gate = 0.0f;
};

/// Extract the descriptors above from an already analysed frame.
VoiceFeatures extract_voice_features(const AnalysisFrame& frame);

/// Decide how voice-like a frame is, in [0, 1].
///
/// A single "share of energy in the speech band" test cannot separate voice from noise:
/// white noise puts roughly half its energy there, and any steady tone inside 300-4000 Hz
/// puts all of it there. Four descriptors are required:
///   * spectral tilt rejects white noise (flat spectrum, tilt near zero);
///   * vocal peakiness rejects steady tones (all vocal power in one band);
///   * excited-band count rejects narrowband signals that survive the tilt test;
///   * the level gate rejects frames too quiet to judge.
/// Thresholds are named constants in controller.cpp and are meant to be re-calibrated
/// against real recordings.
float voice_likelihood_from_features(const VoiceFeatures& features);

/// On-device listening-context model output (v0.2-s3, 92-dim window).
///
/// Written by the audio task right after `AdaptController::update()`, never by the controller
/// itself: Round 2 of `docs/integration-plan.md` brings the model on device as **telemetry**
/// (screen + serial JSON) and deliberately changes no decision. Round 3 is the round that may
/// wire `horn` into `safety_hold`, and it has to extend this contract on purpose.
///
/// Declared here rather than in `context_window.hpp` so `EarbudCommand` carries the model
/// result without the controller depending on the model header.
struct ContextResult {
    /// False until the 2 s window is full, and false again after an input interruption.
    bool valid = false;
    uint8_t class_index = 0;
    /// Softmax confidence of `class_index`, in (0, 1].
    float score = 0.0f;
    /// One forward pass, microseconds.
    int32_t inference_us = 0;
    /// Points at a string literal from `listening_model.h`: static storage, never freed.
    const char* class_name = "unknown";
};

struct EarbudCommand {
    uint32_t schema_version = 2;
    uint32_t sequence = 0;
    uint32_t timestamp_ms = 0;
    float speech_gain_db = 0.0f;
    float external_voice_gain_db = 0.0f;
    uint8_t media_percent = 100;
    std::array<float, kBandCount> eq_gain_db{};
    std::array<float, kBandCount> ambient_band_dbfs{};
    AdaptState state = AdaptState::observing;
    EnvironmentKind environment = EnvironmentKind::quiet;
    float confidence = 0.0f;
    bool earbud_link_connected = false;
    TuningMode tuning_mode = TuningMode::flat;
    ConversationState conversation = ConversationState::none;
    /// Model telemetry. `AdaptController` never touches this field.
    ContextResult context;
};

/// Turns one analysis frame into an `AnalysisFrame`, including the voice decision.
///
/// **Stateful.** The per-frame voice decision alone cannot separate speech from a sustained
/// hum: a hum at f0 ~200 Hz has ~20 harmonics spread over 8-11 of the 24 bands, so the
/// peakiness gate (designed for a single steady tone) never fires and the per-frame product
/// reaches 1.00. Speech and hum differ in the *time* domain, so the analyzer keeps a short
/// history of two descriptors and applies a modulation gate on top of the per-frame score.
/// See `voice_thresholds::kMinLevelStdDb` in controller.cpp for the measured numbers.
class NoiseAnalyzer {
public:
    AnalysisFrame analyze(const int16_t* mono, std::size_t count, uint32_t timestamp_ms,
                          float motion = 0.0f);

    /// Drop the accumulated history. Call when the input stream is interrupted, so a gap
    /// is not mistaken for a steady sound (or for modulation that never happened).
    void reset();

private:
    /// ~1.5 s at the firmware's 100 ms control period.
    static constexpr std::size_t kHistoryFrames = 15;
    /// Frames required before the gate applies. Below this the returned score is exactly
    /// the per-frame product, which is what keeps single-frame callers (and their tests)
    /// behaving as before.
    static constexpr std::size_t kMinHistoryFrames = 5;

    /// Gate in [0, 1]; 1.0 when the sound moves the way speech does. Only ever lowers the
    /// score it is multiplied into.
    float modulation_term() const;

    std::array<float, kHistoryFrames> level_history_{};
    std::array<float, kHistoryFrames> ratio_history_{};
    std::size_t history_count_ = 0;
    std::size_t history_next_ = 0;
};

class AdaptController {
public:
    EarbudCommand update(const AnalysisFrame& frame, float dt_ms);
    void reset();

private:
    uint32_t sequence_ = 0;
    float speech_gain_db_ = 0.0f;
    float media_percent_ = 100.0f;
    float valid_ms_ = 0.0f;
    float invalid_ms_ = 0.0f;
    /// Apple 式对话感知：自身语音触发 → 下压媒体 → 保持 → 恢复。
    /// 触发证据、时序参数与状态机都在 main/conversation_awareness.hpp 里；
    /// 旧实现的轮次启发式（语音→停顿→再语音）已删除，理由见 docs/apple-conversation-awareness.md。
    conversation::Awareness awareness_;
};

const char* adapt_state_name(AdaptState state);
const char* environment_name(EnvironmentKind environment);
const char* tuning_mode_name(TuningMode mode);
const char* conversation_state_name(ConversationState state);
