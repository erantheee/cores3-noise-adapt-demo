#include "mic_decimator.hpp"

#include <algorithm>
#include <cmath>

namespace capture {
namespace {
/// Bilinear (RBJ) low-pass, pre-normalised so the DC gain is exactly 1.
///
/// Generated for a 6400 Hz cutoff at 48 kHz. Verified response of the cascade:
///
///     100 Hz   -0.00 dB        (DC gain exactly 1.000000)
///     1 kHz    -0.08 dB
///     3 kHz    -0.94 dB        speech band essentially untouched
///     4 kHz    -2.04 dB
///     6.4 kHz  -7.45 dB
///     8 kHz   -12.94 dB        the fold frequency
///     12 kHz  -29.05 dB
///     16 kHz  -47.45 dB
///
/// Two sections at 12 dB per octave each. A single section left alias products only
/// 6 dB down at the fold, which is not enough separation for embedding features.
///
/// The 2 dB droop at 4 kHz is a deliberate trade against alias rejection: pushing the
/// cutoff up to reduce droop in the speech band also lets more of the 8-24 kHz region fold
/// back onto it. It is also a constant, linear effect on every recording, so it cancels in
/// any comparison between recordings.
struct BiquadCoefficients {
    float b0, b1, b2, a1, a2;
};

constexpr BiquadCoefficients kSection1{
    0.108447081f, 0.216894161f, 0.108447081f, -0.877267734f, 0.311056057f};
constexpr BiquadCoefficients kSection2{
    0.102165126f, 0.204330252f, 0.102165126f, -0.826450728f, 0.235111232f};

void load(Biquad& target, const BiquadCoefficients& source) {
    target.b0 = source.b0;
    target.b1 = source.b1;
    target.b2 = source.b2;
    target.a1 = source.a1;
    target.a2 = source.a2;
}
}  // namespace

float Biquad::process(float x) {
    // Direct form II transposed: better numerical behaviour with float state than
    // direct form I, and only two state variables.
    const float y = b0 * x + z1;
    z1 = b1 * x - a1 * y + z2;
    z2 = b2 * x - a2 * y;
    return y;
}

void Biquad::reset() { z1 = 0.0f; z2 = 0.0f; }

AntiAliasFilter::AntiAliasFilter() {
    load(first, kSection1);
    load(second, kSection2);
    reset();
}

void AntiAliasFilter::reset() {
    first.reset();
    second.reset();
}

float AntiAliasFilter::process(float x) { return second.process(first.process(x)); }

Decimator::Decimator() { reset(); }

void Decimator::reset() {
    filter_.reset();
    phase_ = 0;
}

std::size_t Decimator::process(const int16_t* input, std::size_t count, int16_t* output,
                               std::size_t output_capacity) {
    if (input == nullptr || output == nullptr || count == 0) return 0;

    // Emit every kDecimation-th filtered sample. The phase is carried across calls so a
    // block boundary never shifts the output alignment.
    const std::size_t worst_case = (count + phase_ + kDecimation - 1) / kDecimation;
    if (output_capacity < worst_case) return 0;

    std::size_t written = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const float filtered = filter_.process(static_cast<float>(input[i]));
        if (++phase_ == kDecimation) {
            phase_ = 0;
            // The filter output is bounded by the input range, so the cast cannot wrap.
            const float clamped = std::max(-32768.0f, std::min(32767.0f, filtered));
            output[written++] = static_cast<int16_t>(std::lround(clamped));
        }
    }
    return written;
}

}  // namespace capture
