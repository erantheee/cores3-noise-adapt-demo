// Tests for the 48 kHz -> 16 kHz capture decimator.
//
// Two failure modes here would corrupt every measurement the project makes and neither
// would crash:
//
//   1. Aliasing. 8-24 kHz content folded back onto the speech band looks like real signal
//      and would show up as extra "noise" or as false formant energy. The first test below
//      drives a tone above the fold frequency and checks that it does not survive.
//   2. A DC gain that is not exactly 1. Any offset shifts recorded levels, and since the
//      near-field signal-to-noise account is a comparison of levels, a constant gain error
//      would bias the headline number. The second test pins the gain.
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>
#include "../main/mic_decimator.hpp"

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

constexpr double kInputRate = 48000.0;

std::vector<int16_t> tone(double freq_hz, double amplitude, std::size_t count) {
    std::vector<int16_t> out(count);
    for (std::size_t i = 0; i < count; ++i) {
        const double value = amplitude * std::sin(2.0 * M_PI * freq_hz *
                                                  static_cast<double>(i) / kInputRate);
        out[i] = static_cast<int16_t>(std::lround(value));
    }
    return out;
}

double rms(const std::vector<int16_t>& x, std::size_t skip = 0) {
    if (x.size() <= skip) return 0.0;
    double sum = 0.0;
    for (std::size_t i = skip; i < x.size(); ++i) {
        sum += static_cast<double>(x[i]) * static_cast<double>(x[i]);
    }
    return std::sqrt(sum / static_cast<double>(x.size() - skip));
}

double dbfs(double value) { return 20.0 * std::log10(std::max(value, 1.0e-9)); }
}  // namespace

int main() {
    using capture::Decimator;

    // --- Output rate ----------------------------------------------------------------
    {
        Decimator decimator;
        std::vector<int16_t> input(4800, 0);
        std::vector<int16_t> output(4800, 0);
        const std::size_t produced =
            decimator.process(input.data(), input.size(), output.data(), output.size());
        check(produced == 1600, "4800 input samples at 48 kHz become 1600 at 16 kHz");
    }

    // --- A tone inside the passband must survive with its level intact ---------------
    {
        Decimator decimator;
        const auto input = tone(1000.0, 10000.0, 48000);
        std::vector<int16_t> output(48000, 0);
        const std::size_t produced =
            decimator.process(input.data(), input.size(), output.data(), output.size());
        output.resize(produced);
        // Skip the filter's start-up transient.
        const double before = dbfs(rms(input, 1000));
        const double after = dbfs(rms(output, 400));
        check(std::fabs(before - after) < 0.5,
              "a 1 kHz tone keeps its level through decimation (within 0.5 dB)");
    }

    // --- Anti-aliasing: content above the fold must not survive ---------------------
    //
    // 12 kHz folds to 4 kHz at a 16 kHz output rate, which lands squarely in the speech
    // band. Without the anti-alias filter this tone would appear as a strong 4 kHz signal.
    {
        Decimator decimator;
        const auto input = tone(12000.0, 20000.0, 48000);
        std::vector<int16_t> output(48000, 0);
        const std::size_t produced =
            decimator.process(input.data(), input.size(), output.data(), output.size());
        output.resize(produced);
        const double attenuation = dbfs(rms(output, 400)) - dbfs(rms(input, 1000));
        check(attenuation < -25.0,
              "a 12 kHz tone is attenuated by more than 25 dB rather than folding into band");
    }
    {
        Decimator decimator;
        const auto input = tone(20000.0, 20000.0, 48000);
        std::vector<int16_t> output(48000, 0);
        const std::size_t produced =
            decimator.process(input.data(), input.size(), output.data(), output.size());
        output.resize(produced);
        const double attenuation = dbfs(rms(output, 400)) - dbfs(rms(input, 1000));
        check(attenuation < -40.0, "a 20 kHz tone is attenuated by more than 40 dB");
    }

    // --- DC gain must be exactly unity ----------------------------------------------
    {
        Decimator decimator;
        std::vector<int16_t> input(48000, 8000);
        std::vector<int16_t> output(48000, 0);
        const std::size_t produced =
            decimator.process(input.data(), input.size(), output.data(), output.size());
        output.resize(produced);
        // A constant input is DC, so it must come out at the same value once settled.
        check(output.back() == 8000,
              "a constant input passes through at the same value (DC gain is unity)");
    }

    // --- Phase is carried across blocks ---------------------------------------------
    //
    // If the phase reset per call, a caller feeding 2048-sample blocks would get a
    // different output rate than one feeding 4096-sample blocks, and the recording would
    // be resampled inconsistently.
    {
        Decimator split;
        Decimator whole;
        std::vector<int16_t> input(48000);
        for (std::size_t i = 0; i < input.size(); ++i) {
            input[i] = static_cast<int16_t>(std::lround(9000.0 * std::sin(
                2.0 * M_PI * 700.0 * static_cast<double>(i) / kInputRate)));
        }

        std::vector<int16_t> whole_out(48000, 0);
        const std::size_t whole_n =
            whole.process(input.data(), input.size(), whole_out.data(), whole_out.size());

        std::vector<int16_t> split_out;
        const std::size_t block = 2048;
        for (std::size_t offset = 0; offset < input.size(); offset += block) {
            const std::size_t count = std::min(block, input.size() - offset);
            std::vector<int16_t> stage(block, 0);
            const std::size_t n = split.process(input.data() + offset, count, stage.data(),
                                                stage.size());
            split_out.insert(split_out.end(), stage.begin(),
                             stage.begin() + static_cast<std::ptrdiff_t>(n));
        }

        check(whole_n == split_out.size(), "block size does not change the output rate");
        check(whole_out.size() >= whole_n &&
                  std::equal(split_out.begin(), split_out.end(), whole_out.begin()),
              "block size does not change the output samples");
    }

    // --- Output buffer too small is refused, not truncated --------------------------
    {
        Decimator decimator;
        std::vector<int16_t> input(4800, 0);
        std::vector<int16_t> tiny(8, 0);
        check(decimator.process(input.data(), input.size(), tiny.data(), tiny.size()) == 0,
              "a short output buffer is refused rather than partially filled");
        check(decimator.process(nullptr, 100, tiny.data(), tiny.size()) == 0,
              "a null input is refused");
        check(decimator.process(input.data(), 0, tiny.data(), tiny.size()) == 0,
              "an empty block is refused");
    }

    // --- Reset returns the filter to a known state ----------------------------------
    {
        Decimator decimator;
        const auto impulse = [](std::size_t n) {
            std::vector<int16_t> x(n, 0);
            x[0] = 32767;
            return x;
        };
        const auto impulse_48k = impulse(48000);
        std::vector<int16_t> first(48000, 0);
        const std::size_t first_n =
            decimator.process(impulse_48k.data(), impulse_48k.size(), first.data(), first.size());
        check(first_n > 0, "an impulse produces output");
        decimator.reset();
        std::vector<int16_t> second(48000, 0);
        const std::size_t second_n =
            decimator.process(impulse_48k.data(), impulse_48k.size(), second.data(), second.size());
        check(second_n == first_n &&
                  std::equal(first.begin(),
                             first.begin() + static_cast<std::ptrdiff_t>(second_n),
                             second.begin()),
              "after reset the filter reproduces the same response");
    }

    std::cout << "MicDecimator: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
