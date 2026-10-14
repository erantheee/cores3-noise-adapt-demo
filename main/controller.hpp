#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

constexpr std::size_t kBandCount = 24;
/// Largest analysis frame the window table will hold. The audio task uses 2048 samples.
constexpr std::size_t kMaxAnalysisFrame = 4096;
constexpr float kMaxSpeechGainDb = 6.5f;
constexpr float kMaxNoiseReductionDb = 8.0f;

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

struct EarbudCommand {
    uint32_t schema_version = 1;
    uint32_t sequence = 0;
    uint32_t timestamp_ms = 0;
    float speech_gain_db = 0.0f;
    float noise_reduction_db = 0.0f;
    uint8_t media_percent = 100;
    std::array<float, kBandCount> eq_gain_db{};
    AdaptState state = AdaptState::observing;
    EnvironmentKind environment = EnvironmentKind::quiet;
    float confidence = 0.0f;
    bool earbud_link_connected = false;
};

class NoiseAnalyzer {
public:
    AnalysisFrame analyze(const int16_t* mono, std::size_t count, uint32_t timestamp_ms,
                          float motion = 0.0f) const;
};

class AdaptController {
public:
    EarbudCommand update(const AnalysisFrame& frame, float dt_ms);
    void reset();

private:
    uint32_t sequence_ = 0;
    float speech_gain_db_ = 0.0f;
    float noise_reduction_db_ = 0.0f;
    float media_percent_ = 100.0f;
    float valid_ms_ = 0.0f;
    float invalid_ms_ = 0.0f;
};

const char* adapt_state_name(AdaptState state);
const char* environment_name(EnvironmentKind environment);
