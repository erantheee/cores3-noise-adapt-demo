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

float slew(float current, float target, float dt_ms, float attack_ms, float release_ms) {
    const float tau = target > current ? attack_ms : release_ms;
    const float alpha = 1.0f - std::exp(-std::max(dt_ms, 0.0f) / std::max(tau, 1.0f));
    return current + (target - current) * alpha;
}

float goertzel_dbfs(const int16_t* samples, std::size_t count, float frequency) {
    const float omega = 2.0f * static_cast<float>(M_PI) * frequency / kSampleRate;
    const float coeff = 2.0f * std::cos(omega);
    float s1 = 0.0f;
    float s2 = 0.0f;
    for (std::size_t i = 0; i < count; ++i) {
        const float window = 0.5f - 0.5f * std::cos(2.0f * static_cast<float>(M_PI) * i /
                                                    std::max<std::size_t>(count - 1, 1));
        const float x = (static_cast<float>(samples[i]) / 32768.0f) * window;
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
    std::size_t clipped = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const float x = static_cast<float>(mono[i]) / 32768.0f;
        sum += x * x;
        if (std::abs(mono[i]) >= 32760) ++clipped;
    }
    const float rms = std::sqrt(static_cast<float>(sum / count));
    frame.broadband_dbfs = 20.0f * std::log10(std::max(rms, 1.0e-6f));
    frame.mic_valid = std::isfinite(frame.broadband_dbfs) && clipped < count / 100;

    float vocal_power = 0.0f;
    float total_power = 0.0f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        frame.band_dbfs[band] = goertzel_dbfs(mono, count, kBandCenter[band]);
        const float power = std::pow(10.0f, frame.band_dbfs[band] / 10.0f);
        total_power += power;
        if (kBandCenter[band] >= 300.0f && kBandCenter[band] <= 4000.0f) vocal_power += power;
    }
    const float vocal_ratio = vocal_power / std::max(total_power, 1.0e-12f);
    const float level_gate = clamp01((frame.broadband_dbfs + 58.0f) / 18.0f);
    frame.voice_likelihood = clamp01((vocal_ratio - 0.34f) / 0.36f) * level_gate;
    return frame;
}

EarbudCommand AdaptController::update(const AnalysisFrame& frame, float dt_ms) {
    EarbudCommand command;
    command.sequence = ++sequence_;
    command.timestamp_ms = frame.timestamp_ms;
    command.eq_gain_db.fill(0.0f);

    if (!frame.mic_valid || !std::isfinite(frame.broadband_dbfs)) {
        invalid_ms_ += dt_ms;
        valid_ms_ = 0.0f;
        speech_gain_db_ = slew(speech_gain_db_, 0.0f, dt_ms, 100.0f, 250.0f);
        noise_reduction_db_ = slew(noise_reduction_db_, 0.0f, dt_ms, 100.0f, 250.0f);
        media_percent_ = slew(media_percent_, 100.0f, dt_ms, 100.0f, 250.0f);
        command.state = invalid_ms_ >= 250.0f ? AdaptState::mic_fault : AdaptState::recovering;
    } else {
        invalid_ms_ = 0.0f;
        valid_ms_ += dt_ms;
        const float noise = clamp01((frame.broadband_dbfs + 58.0f) / 30.0f);
        const float voice = clamp01(frame.voice_likelihood);
        const float speech_target = std::min(kMaxSpeechGainDb, noise * (2.5f + 4.0f * voice));
        const float nr_target = std::min(kMaxNoiseReductionDb, noise * (7.0f - 2.0f * voice));
        const float media_target = 100.0f - 28.0f * noise * voice;
        speech_gain_db_ = slew(speech_gain_db_, speech_target, dt_ms, 350.0f, 1200.0f);
        noise_reduction_db_ = slew(noise_reduction_db_, nr_target, dt_ms, 450.0f, 1500.0f);
        media_percent_ = slew(media_percent_, media_target, dt_ms, 250.0f, 800.0f);

        if (frame.broadband_dbfs < -56.0f) command.environment = EnvironmentKind::quiet;
        else if (voice > 0.55f) command.environment = EnvironmentKind::voice_likely;
        else if (frame.broadband_dbfs > -30.0f) command.environment = EnvironmentKind::loud;
        else command.environment = EnvironmentKind::steady_noise;

        const bool moving = std::abs(speech_target - speech_gain_db_) > 0.25f ||
                            std::abs(nr_target - noise_reduction_db_) > 0.25f;
        command.state = valid_ms_ < 500.0f ? AdaptState::observing
                                           : (moving ? AdaptState::adjusting : AdaptState::holding);
        command.confidence = clamp01(valid_ms_ / 1200.0f) * (1.0f - clamp01(frame.motion / 4.0f) * 0.35f);

        for (std::size_t band = 0; band < kBandCount; ++band) {
            const bool speech_band = band >= 8 && band <= 16;
            command.eq_gain_db[band] = speech_band ? speech_gain_db_ : 0.0f;
        }
    }

    command.speech_gain_db = std::clamp(speech_gain_db_, 0.0f, kMaxSpeechGainDb);
    command.noise_reduction_db = std::clamp(noise_reduction_db_, 0.0f, kMaxNoiseReductionDb);
    command.media_percent = static_cast<uint8_t>(std::lround(std::clamp(media_percent_, 65.0f, 100.0f)));
    return command;
}

void AdaptController::reset() {
    sequence_ = 0;
    speech_gain_db_ = 0.0f;
    noise_reduction_db_ = 0.0f;
    media_percent_ = 100.0f;
    valid_ms_ = 0.0f;
    invalid_ms_ = 0.0f;
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
