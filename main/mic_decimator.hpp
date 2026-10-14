#pragma once

#include <cstddef>
#include <cstdint>

/// Microphone capture: 48 kHz device stream down to 16 kHz chunks for the serial link.
///
/// The device samples at 48 kHz because the 24-band analysis needs the full band. The
/// capture path does not: speaker-embedding models are trained at 16 kHz, and the USB
/// serial console cannot carry 48 kHz anyway. Measured link budget on the default console
/// (921600 baud, roughly 92 kB/s after framing):
///
///     48 kHz mono 16-bit, base64   128 kB/s   139% of the link   unusable
///     16 kHz mono 16-bit, base64    43 kB/s    46% of the link   usable
///
/// So decimation is a requirement, not a convenience. Decimating by taking every third
/// sample would alias; the anti-alias filter below runs at 48 kHz before the drop.
///
/// This is the same construction a real voice path needs later, which is why it lives in
/// its own module rather than inline in the capture loop.
namespace capture {

constexpr uint32_t kInputRate = 48000;
constexpr uint32_t kOutputRate = 16000;
constexpr uint32_t kDecimation = kInputRate / kOutputRate;

static_assert(kInputRate % kOutputRate == 0, "the input rate must divide evenly");

/// Second-order section, direct form II transposed. Kept separate so the filter can be
/// tested without a device.
struct Biquad {
    float b0 = 1.0f, b1 = 0.0f, b2 = 0.0f, a1 = 0.0f, a2 = 0.0f;
    float z1 = 0.0f, z2 = 0.0f;

    float process(float x);
    void reset();
};

/// 8 kHz cutoff low-pass, two cascaded sections at 48 kHz.
///
/// A single biquad rolls off 12 dB per octave, which leaves audible energy folded back
/// into the top of the 8 kHz output band. Two sections give 24 dB per octave and put the
/// alias products far enough down for embedding features not to care.
struct AntiAliasFilter {
    Biquad first;
    Biquad second;

    AntiAliasFilter();
    void reset();
    float process(float x);
};

/// Decimates a 48 kHz block into a 16 kHz block.
///
/// Holds filter state across calls, so the same instance must be used for a whole
/// recording. Restarting the filter mid-recording would produce a step discontinuity.
class Decimator {
public:
    Decimator();

    /// Convert `count` input samples. Returns the number of output samples written.
    /// Returns 0 when the inputs are unusable or the output buffer is too short; it never
    /// writes a partial result, because a short block would shift every later sample.
    std::size_t process(const int16_t* input, std::size_t count, int16_t* output,
                        std::size_t output_capacity);

    void reset();

private:
    AntiAliasFilter filter_;
    uint32_t phase_ = 0;
};

}  // namespace capture
