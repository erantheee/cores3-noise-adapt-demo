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
#include <algorithm>
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

// ── Sequence helpers ────────────────────────────────────────────────────────────
//
// The voice decision is stateful, so anything about a *sustained* sound has to be measured
// over a sequence driven through ONE analyzer. `voice_score` above deliberately does the
// opposite (fresh analyzer, one frame) because that is what the original per-frame cases
// pin; both are needed and they must not be confused.

/// Analysis frame the firmware actually uses: 512 samples per read, five reads = 2560.
constexpr std::size_t kSequenceFrame = 2560;
/// NoiseAnalyzer::kMinHistoryFrames. Frames before this cannot be judged for steadiness.
constexpr int kWarmupFrames = 5;

int sequence_crossings(const std::vector<float>& signal, float dbfs, int skip_frames = 0) {
    NoiseAnalyzer analyzer;
    const auto pcm = quantise(signal, dbfs);
    const std::size_t frames = pcm.size() / kSequenceFrame;
    int over = 0;
    for (std::size_t k = 0; k < frames; ++k) {
        const auto frame = analyzer.analyze(pcm.data() + k * kSequenceFrame, kSequenceFrame,
                                            static_cast<uint32_t>(k * 100));
        if (static_cast<int>(k) >= skip_frames && frame.voice_likelihood >= kVoiceCut) ++over;
    }
    return over;
}

float sequence_median(const std::vector<float>& signal, float dbfs) {
    NoiseAnalyzer analyzer;
    const auto pcm = quantise(signal, dbfs);
    const std::size_t frames = pcm.size() / kSequenceFrame;
    std::vector<float> scores;
    scores.reserve(frames);
    for (std::size_t k = 0; k < frames; ++k) {
        scores.push_back(analyzer.analyze(pcm.data() + k * kSequenceFrame, kSequenceFrame,
                                          static_cast<uint32_t>(k * 100))
                             .voice_likelihood);
    }
    if (scores.empty()) return 0.0f;
    std::sort(scores.begin(), scores.end());
    return scores[scores.size() / 2];
}

/// Sustained closed-mouth vocalisation: glottal harmonics through two low formants.
/// **No modulation at all** — that is the structural difference from speech, and the whole
/// point of the case.
std::vector<float> hum_sequence(float f0, float rolloff, float formant_hz, float seconds,
                                uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    const std::size_t n = static_cast<std::size_t>(seconds * kSampleRate);
    std::vector<float> out(n);
    Resonator r1, r2;
    r1.set(formant_hz, 260.0f);
    r2.set(formant_hz * 2.4f, 420.0f);
    const int harmonics = static_cast<int>(3500.0f / f0);
    for (std::size_t i = 0; i < n; ++i) {
        const float t = static_cast<float>(i) / kSampleRate;
        float glottal = 0.0f;
        for (int h = 1; h <= harmonics; ++h) {
            glottal += std::sin(2.0f * float(M_PI) * f0 * h * t) /
                       std::pow(static_cast<float>(h), rolloff);
        }
        glottal += 0.02f * normal(rng);
        out[i] = r1.process(glottal) + 0.5f * r2.process(glottal);
    }
    normalise_band(out, 300.0f, 4000.0f, -20.0f);
    return out;
}

/// Turbulent airflow, shaped and gated at respiration rate. No harmonic structure.
std::vector<float> breath_sequence(float hp_coeff, float rumble_mix, float rate_hz,
                                   float seconds, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    const std::size_t n = static_cast<std::size_t>(seconds * kSampleRate);
    std::vector<float> out(n);
    float hpf = 0.0f, prev = 0.0f, rumble = 0.0f;
    for (std::size_t i = 0; i < n; ++i) {
        const float t = static_cast<float>(i) / kSampleRate;
        const float nz = normal(rng);
        hpf = hp_coeff * (hpf + nz - prev);
        prev = nz;
        rumble = 0.995f * rumble + 0.005f * nz;
        const float env = 0.5f - 0.5f * std::cos(2.0f * float(M_PI) * rate_hz * t);
        out[i] = (hpf + rumble_mix * rumble) * env;
    }
    normalise_band(out, 300.0f, 4000.0f, -20.0f);
    return out;
}

/// Continuous speech: syllable-rate modulation, moving formants, gated fricatives.
std::vector<float> speech_sequence(float seconds, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    const std::size_t n = static_cast<std::size_t>(seconds * kSampleRate);
    std::vector<float> out(n);
    Resonator f1, f2, f3;
    float hpf = 0.0f, prev = 0.0f;
    for (std::size_t i = 0; i < n; ++i) {
        const float t = static_cast<float>(i) / kSampleRate;
        f1.set(500.0f + 300.0f * std::sin(2.0f * float(M_PI) * 0.7f * t), 160.0f);
        f2.set(1300.0f + 500.0f * std::sin(2.0f * float(M_PI) * 0.5f * t + 1.0f), 200.0f);
        f3.set(2500.0f + 400.0f * std::sin(2.0f * float(M_PI) * 0.3f * t), 320.0f);
        const float g = normal(rng);
        const float voiced = f1.process(g) + 0.6f * f2.process(g) + 0.3f * f3.process(g);
        const float syllable = 0.5f - 0.5f * std::cos(2.0f * float(M_PI) * 4.0f * t);
        const float nz = normal(rng);
        hpf = 0.93f * (hpf + nz - prev);
        prev = nz;
        const float blink = syllable > 0.72f ? 1.0f : 0.0f;
        out[i] = voiced * (0.6f + 0.4f * syllable) + 0.35f * hpf * blink;
    }
    normalise_band(out, 300.0f, 4000.0f, -20.0f);
    return out;
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

    // --- Sustained sounds: the per-frame product is not enough ---------------------
    // Measured failure: EVERY frame of a hum at f0 ~200 Hz scored 1.0000, because ~20
    // harmonics spaced 200 Hz apart land in 8-11 of the 24 bands and peakiness stays under
    // its 0.72 ceiling. See the temporal-gate note in controller.cpp.
    //
    // These cases drive a SEQUENCE through one analyzer, which is what the firmware does.
    // A fresh analyzer per frame would leave the gate permanently at 1.0 and pin the old,
    // broken behaviour instead. That mistake was made once already: the first version of
    // tools/voice_decision_sweep.cpp measured single frames taken from the start of a
    // signal — the quiet half of a breathing cycle — and reported breath as safe.
    {
        const auto steady_hum = hum_sequence(200.0f, 1.0f, 600.0f, 6.0f, 4001);
        check(sequence_median(steady_hum, -30.0f) < kVoiceCut,
              "a sustained hum at 200 Hz is not voice (median over the sequence)");
        check(sequence_crossings(steady_hum, -30.0f) <= kWarmupFrames,
              "a sustained hum crosses the cut only while the history is filling");
    }
    {
        const auto low_hum = hum_sequence(110.0f, 1.2f, 600.0f, 6.0f, 4002);
        check(sequence_median(low_hum, -30.0f) < kVoiceCut,
              "a sustained low hum at 110 Hz is not voice");
        check(sequence_crossings(low_hum, -30.0f) <= kWarmupFrames,
              "a sustained low hum crosses the cut only while the history is filling");
    }
    // A hum that arrives after silence must be gated from its first frames, not after a
    // whole window: with a variance statistic the single silence-to-sound jump held the
    // gate open and 14 of 92 frames crossed. The median of successive differences is what
    // makes this pass; see the note on kMinLevelStepDb.
    {
        std::vector<float> after_silence(kSampleRate * 2, 0.0f);
        const auto hum = hum_sequence(200.0f, 1.0f, 600.0f, 4.0f, 4003);
        after_silence.insert(after_silence.end(), hum.begin(), hum.end());
        check(sequence_crossings(after_silence, -30.0f, /*skip_frames=*/45) == 0,
              "a hum arriving after silence is gated immediately, not after a window");
    }
    // Speech must survive the same gate. This is the half that keeps the fix from simply
    // trading one false negative for another.
    {
        const auto speech = speech_sequence(6.0f, 11);
        check(sequence_crossings(speech, -30.0f) > 10,
              "speech still crosses the cut after the temporal gate is added");
    }

    // --- Breathing: measured, still open -------------------------------------------
    // A breath burst is amplitude-modulated (0.2-1 Hz respiration), so the temporal gate
    // does not reject it: the gate measures HOW MUCH the level moves, not HOW FAST. On the
    // loud half of a burst the per-frame product reaches 1.00 and up to 23 of 75 frames
    // crossed the cut. This asserts only the median; the tail is tracked as open item 35
    // in docs/plan-status.md and is expected to need a modulation-RATE term (syllable rate
    // 2-8 Hz against respiration 0.2-1 Hz), not a deeper depth threshold.
    {
        const auto breath = breath_sequence(0.86f, 0.4f, 0.6f, 6.0f, 5001);
        check(sequence_median(breath, -25.0f) < kVoiceCut,
              "the median frame of a breath burst is not voice");
    }

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

    // --- Keyboard transient detection (D53) ---------------------------------------
    // 键盘点击 = 帧内 2–15 ms 的宽带瞬态：2048 样本分 8 个 5.3 ms 子块后，最响子块的
    // 一阶差分能量（≈4 kHz 以上）明显高于中位子块；稳态噪声/音乐在各子块间是均匀的。
    // 阈值 9 dB 来自 2026-10-28 现场（肩上打字 2–4 次/s vs 停手 0.2–0.6 次/s）。
    {
        NoiseAnalyzer analyzer;
        const std::vector<float> steady = white_signal();
        const auto pcm = quantise(steady, -30.0f);
        const AnalysisFrame frame = analyzer.analyze(pcm.data(), pcm.size(), 0);
        check(!frame.click_transient, "steady noise is not a keyboard transient");
        check(frame.hf_crest_db < 9.0f, "steady noise keeps the HF crest below the threshold");
    }
    {
        NoiseAnalyzer analyzer;
        const std::vector<float> steady = white_signal();
        auto pcm = quantise(steady, -40.0f);
        const auto add_click = [&pcm](std::size_t start, std::size_t length) {
            for (std::size_t i = 0; i < length; ++i) {
                pcm[start + i] = (i % 2 == 0) ? 24000 : -24000;
            }
        };
        add_click(300, 96);    // ~2 ms，落在第 2 个子块
        add_click(1300, 96);   // ~2 ms，落在第 6 个子块
        const AnalysisFrame frame = analyzer.analyze(pcm.data(), pcm.size(), 0);
        check(frame.click_transient,
              "a keyboard-sized HF transient inside the frame is detected");
        check(frame.mic_valid, "a short transient does not invalidate the microphone");
    }
    {
        NoiseAnalyzer analyzer;
        std::vector<int16_t> silence(kFrame, 0);
        const AnalysisFrame frame = analyzer.analyze(silence.data(), silence.size(), 0);
        check(!frame.click_transient, "digital silence is not a keyboard transient");
    }

    std::cout << "NoiseAnalyzer: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
