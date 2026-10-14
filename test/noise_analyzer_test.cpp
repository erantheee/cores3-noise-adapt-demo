// Tests for NoiseAnalyzer: the voice decision and the clipping detector.
//
// Before this suite existed, NoiseAnalyzer had zero coverage. The environment decision is
// the product's core claim, so its behaviour is now pinned by named cases rather than by
// whatever the code happened to do.
//
// The reference cases below use synthetic signals generated from spectral targets. WARNING
// on scope: these pin the *shape* of the decision and prove the known false positives are
// gone. They do not establish accuracy on real speech. Thresholds marked CALIBRATION in
// controller.cpp still have to be re-fitted against real recordings; see docs/.
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>
#include <vector>
#include "../main/controller.hpp"

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const std::string& name) {
    if (condition) {
        ++passed;
        std::cout << "PASS " << name << '\n';
    } else {
        ++failed;
        std::cout << "FAIL " << name << '\n';
    }
}

constexpr float kSampleRate = 48000.0f;
constexpr std::size_t kFrame = 2048;

/// Every generator owns a seeded engine. Sharing one engine would make each test depend on
/// how many random draws earlier tests happened to consume, which is how this suite first
/// produced a result that changed from run to run.
std::mt19937 g_rng(20261014);
std::normal_distribution<float> g_normal(0.0f, 1.0f);
constexpr uint32_t kSpeechSeed = 1001;
constexpr uint32_t kWhiteSeed = 2002;
constexpr uint32_t kPinkSeed = 3003;

/// Second-order resonator, direct form I.
struct Resonator {
    float b0 = 0.0f, a1 = 0.0f, a2 = 0.0f, z1 = 0.0f, z2 = 0.0f;
    void set(float freq_hz, float bandwidth_hz) {
        const float r = std::exp(-float(M_PI) * bandwidth_hz / kSampleRate);
        const float theta = 2.0f * float(M_PI) * freq_hz / kSampleRate;
        a1 = -2.0f * r * std::cos(theta);
        a2 = r * r;
        b0 = (1.0f - r) * std::sqrt(1.0f - 2.0f * r * std::cos(2.0f * theta) + r * r);
    }
    float process(float x) {
        const float y = b0 * x - a1 * z1 - a2 * z2;
        z2 = z1;
        z1 = y;
        return y;
    }
};

/// Measure mean power in a frequency band with a bank of Goertzels, then scale to target.
void normalise_band(std::vector<float>& x, float lo_hz, float hi_hz, float target_dbfs) {
    double power = 0.0;
    int bins = 0;
    for (float f = lo_hz; f <= hi_hz; f *= 1.15f) {
        const float omega = 2.0f * float(M_PI) * f / kSampleRate;
        const float coeff = 2.0f * std::cos(omega);
        float s1 = 0.0f;
        float s2 = 0.0f;
        for (std::size_t i = 0; i < x.size(); ++i) {
            const float s0 = x[i] + coeff * s1 - s2;
            s2 = s1;
            s1 = s0;
        }
        power += std::max(s1 * s1 + s2 * s2 - coeff * s1 * s2, 1.0e-20f);
        ++bins;
    }
    const double mean = power / std::max(bins, 1);
    const float rms = std::sqrt(static_cast<float>(mean)) / static_cast<float>(x.size());
    const float current_db = 20.0f * std::log10(std::max(rms, 1.0e-9f));
    const float gain = std::pow(10.0f, (target_dbfs - current_db) / 20.0f);
    for (float& v : x) v *= gain;
}

/// Speech-shaped test signal: a glottal source through three formants, amplitude-modulated
/// at syllable rate, plus high-passed fricative bursts.
std::vector<float> speech_signal(float inband_snr_db = 15.0f) {
    std::mt19937 rng(kSpeechSeed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    std::vector<float> voiced(kFrame);
    std::vector<float> noise(kFrame);
    Resonator f1, f2, f3;
    f1.set(700.0f, 180.0f);
    f2.set(1220.0f, 220.0f);
    f3.set(2600.0f, 350.0f);
    float hpf = 0.0f;
    float prev = 0.0f;
    for (std::size_t i = 0; i < kFrame; ++i) {
        const float t = static_cast<float>(i) / kSampleRate;
        const float glottal = normal(rng);
        voiced[i] = (f1.process(glottal) + 0.6f * f2.process(glottal) +
                     0.3f * f3.process(glottal)) *
                    (0.6f + 0.4f * std::sin(2.0f * float(M_PI) * 4.0f * t));
        const float n = normal(rng);
        hpf = 0.93f * (hpf + n - prev);
        prev = n;
        // Fricatives are bursts gated at syllable rate, not steady hiss.
        const float gate =
            (0.5f - 0.5f * std::cos(2.0f * float(M_PI) * 4.0f * t)) > 0.72f ? 1.0f : 0.0f;
        noise[i] = hpf * gate;
    }
    // Place the voiced part at a defined in-band SNR above the fricative part, both
    // measured in the speech band, so level and noise floor vary independently.
    normalise_band(voiced, 300.0f, 4000.0f, -20.0f);
    normalise_band(noise, 300.0f, 4000.0f, -20.0f - inband_snr_db);
    std::vector<float> out(kFrame);
    for (std::size_t i = 0; i < kFrame; ++i) out[i] = voiced[i] + noise[i];
    return out;
}

std::vector<float> white_signal() {
    std::mt19937 rng(kWhiteSeed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    std::vector<float> out(kFrame);
    for (float& v : out) v = normal(rng);
    return out;
}

std::vector<float> pink_signal() {
    std::mt19937 rng(kPinkSeed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    std::vector<float> out(kFrame);
    float state = 0.0f;
    for (float& v : out) {
        state = 0.985f * state + 0.015f * normal(rng);
        v = state * 5.0f;
    }
    return out;
}

std::vector<float> tone_signal(float freq_hz) {
    std::vector<float> out(kFrame);
    for (std::size_t i = 0; i < kFrame; ++i) {
        out[i] = std::sin(2.0f * float(M_PI) * freq_hz * static_cast<float>(i) / kSampleRate);
    }
    return out;
}

std::vector<float> harmonics_signal(float f0, int count) {
    std::vector<float> out(kFrame, 0.0f);
    for (int h = 1; h <= count; ++h) {
        for (std::size_t i = 0; i < kFrame; ++i) {
            out[i] += std::sin(2.0f * float(M_PI) * f0 * h * static_cast<float>(i) /
                               kSampleRate) / static_cast<float>(h);
        }
    }
    return out;
}

/// Scale to a target peak level and quantise, clamping overdrive so clipping is testable.
std::vector<int16_t> quantise(const std::vector<float>& x, float dbfs) {
    float peak = 1.0e-9f;
    for (float v : x) peak = std::max(peak, std::abs(v));
    const float gain = std::pow(10.0f, dbfs / 20.0f) / peak;
    std::vector<int16_t> out(x.size());
    for (std::size_t i = 0; i < x.size(); ++i) {
        const float v = std::max(-1.0f, std::min(1.0f, x[i] * gain));
        out[i] = static_cast<int16_t>(std::lround(v * 32767.0f));
    }
    return out;
}

constexpr float kVoiceCut = 0.55f;

float voice_score(const std::vector<float>& signal, float dbfs) {
    NoiseAnalyzer analyzer;
    const auto pcm = quantise(signal, dbfs);
    return analyzer.analyze(pcm.data(), pcm.size(), 0).voice_likelihood;
}
}  // namespace

int main() {
    // --- The three false positives this change exists to remove ---------------------
    // White noise used to score voice_likelihood 0.72-1.00 and be labelled "人声嘈杂",
    // which drove media_percent down to 73-89% in response to noise alone.
    for (float db : {-50.0f, -45.0f, -40.0f, -35.0f, -30.0f, -25.0f, -20.0f}) {
        const float score = voice_score(white_signal(), db);
        check(score < kVoiceCut, "white noise at " + std::to_string(int(db)) +
                                     " dBFS is not voice (score " + std::to_string(score) + ")");
    }

    // A steady tone inside the speech band used to score a full 1.00.
    for (float freq : {200.0f, 500.0f, 1000.0f, 2000.0f, 3000.0f, 6000.0f}) {
        for (float db : {-30.0f, -20.0f}) {
            const float score = voice_score(tone_signal(freq), db);
            check(score < kVoiceCut, "tone " + std::to_string(int(freq)) + " Hz at " +
                                         std::to_string(int(db)) + " dBFS is not voice");
        }
    }

    // --- Real speech must still be detected ----------------------------------------
    for (float db : {-45.0f, -35.0f, -25.0f, -18.0f}) {
        const float score = voice_score(speech_signal(), db);
        check(score >= kVoiceCut, "speech at " + std::to_string(int(db)) +
                                      " dBFS is voice (score " + std::to_string(score) + ")");
    }
    // A noisy scene: the fricative floor sits only 5 dB under the voice. The detector must
    // still find the voice, because "someone talking in a loud room" is the target case.
    check(voice_score(speech_signal(5.0f), -30.0f) >= kVoiceCut,
          "speech over a loud fricative floor is still voice");
    // And with the voice only 3 dB above the floor the result may legitimately be
    // borderline; that boundary belongs to real recordings, not to synthetic signals.

    // --- Other noise classes ------------------------------------------------------
    for (float db : {-40.0f, -30.0f}) {
        check(voice_score(pink_signal(), db) < kVoiceCut,
              "pink noise at " + std::to_string(int(db)) + " dBFS is not voice");
    }
    for (float f0 : {120.0f, 150.0f, 220.0f}) {
        check(voice_score(harmonics_signal(f0, 5), -30.0f) < kVoiceCut,
              "harmonic stack at " + std::to_string(int(f0)) + " Hz is not voice");
    }
    // Documented limitation: a very peaky, narrow-spectrum voice can still be rejected.
    // Recorded so a future threshold change cannot silently make it worse.
    check(voice_score(speech_signal(), -25.0f) >= kVoiceCut,
          "speech at -25 dBFS is voice (known borderline peakiness case)");

    // --- Silence ------------------------------------------------------------------
    {
        const std::vector<float> silence(kFrame, 0.0f);
        check(voice_score(silence, -120.0f) < 0.01f, "silence is not voice");
    }

    // --- Clipping detection -------------------------------------------------------
    // Counting pinned samples alone is wrong: clean content at -1 dBFS reaches full scale
    // on about 26% of samples. A plateau of consecutive pinned samples is the real signal.
    {
        NoiseAnalyzer analyzer;
        std::vector<float> quiet = white_signal();
        const auto quiet_pcm = quantise(quiet, -10.0f);
        check(analyzer.analyze(quiet_pcm.data(), quiet_pcm.size(), 0).mic_valid,
              "loud but unclipped noise keeps the microphone valid");
    }
    {
        NoiseAnalyzer analyzer;
        std::vector<float> loud = white_signal();
        const auto loud_pcm = quantise(loud, -1.0f);
        check(analyzer.analyze(loud_pcm.data(), loud_pcm.size(), 0).mic_valid,
              "noise at -1 dBFS keeps the microphone valid");
    }
    {
        // 3x overload: the waveform is a plateaus-carrying square-ish signal.
        NoiseAnalyzer analyzer;
        std::vector<int16_t> clipped(kFrame);
        for (std::size_t i = 0; i < kFrame; ++i) {
            const float x = 3.0f * std::sin(2.0f * float(M_PI) * 1000.0f *
                                            static_cast<float>(i) / kSampleRate);
            clipped[i] = static_cast<int16_t>(
                std::lround(std::max(-1.0f, std::min(1.0f, x)) * 32767.0f));
        }
        const AnalysisFrame frame = analyzer.analyze(clipped.data(), clipped.size(), 0);
        check(!frame.mic_valid, "a hard-clipped frame invalidates the microphone");
    }
    {
        // A square wave is the limiting case of clipping.
        NoiseAnalyzer analyzer;
        std::vector<int16_t> square(kFrame);
        for (std::size_t i = 0; i < kFrame; ++i) {
            square[i] = std::sin(2.0f * float(M_PI) * 500.0f * static_cast<float>(i) /
                                 kSampleRate) > 0.0f
                            ? 32767
                            : -32767;
        }
        check(!analyzer.analyze(square.data(), square.size(), 0).mic_valid,
              "a square wave invalidates the microphone");
    }

    // --- Frame hygiene -------------------------------------------------------------
    {
        NoiseAnalyzer analyzer;
        check(analyzer.analyze(nullptr, 0, 0).mic_valid == false,
              "a null buffer is not valid");
        std::vector<int16_t> tiny(64, 0);
        check(analyzer.analyze(tiny.data(), tiny.size(), 0).mic_valid == false,
              "a frame shorter than the minimum is not valid");
    }
    {
        NoiseAnalyzer analyzer;
        std::vector<float> sig = speech_signal(120.0f);
        const auto pcm = quantise(sig, -30.0f);
        const AnalysisFrame frame = analyzer.analyze(pcm.data(), pcm.size(), 1234);
        check(frame.timestamp_ms == 1234, "the timestamp is carried through");
        bool centers_ascending = true;
        for (std::size_t i = 1; i < kBandCount; ++i) {
            if (frame.band_center_hz[i] <= frame.band_center_hz[i - 1]) centers_ascending = false;
        }
        check(centers_ascending && frame.band_center_hz[0] > 0.0f,
              "band centre frequencies are populated and ascending");
    }

    std::cout << "NoiseAnalyzer: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
