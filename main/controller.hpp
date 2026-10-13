#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

constexpr std::size_t kBandCount = 24;
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
    bool mic_valid = false;
};

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
