// Tests for the capture transport.
//
// This module sits between the device's microphone and every measurement the project
// depends on. A silent bug here does not crash anything: it produces recordings that look
// plausible and are wrong, and every separability and signal-to-noise conclusion drawn
// from them would be invalid. The round-trip and rejection cases below are therefore the
// point of the file, not decoration.
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <random>
#include <string>
#include <vector>
#include "../main/mic_capture.hpp"

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

std::string encode(const std::vector<int16_t>& pcm) {
    std::vector<char> buffer(mic_capture::kMaxEncodedChars);
    const std::size_t length =
        mic_capture::encode_chunk(pcm.data(), pcm.size(), buffer.data(), buffer.size());
    return std::string(buffer.data(), length);
}
}  // namespace

int main() {
    using mic_capture::decode_chunk;
    using mic_capture::encode_chunk;

    // --- Round trip over many lengths, including the ones that need padding ----------
    {
        std::mt19937 rng(7);
        std::uniform_int_distribution<int> dist(-32768, 32767);
        bool all_ok = true;
        bool tested_padding_cases = false;
        for (std::size_t count = 1; count <= 400; ++count) {
            std::vector<int16_t> original(count);
            for (auto& v : original) v = static_cast<int16_t>(dist(rng));

            const std::string encoded = encode(original);
            std::vector<int16_t> decoded(count + 8, 0);
            const std::size_t produced =
                decode_chunk(encoded.c_str(), encoded.size(), decoded.data(), decoded.size());
            if (produced != count || std::memcmp(original.data(), decoded.data(), count * 2) != 0) {
                all_ok = false;
                break;
            }
            // Lengths where sample bytes are not a multiple of three exercise the '='
            // path: two bytes per sample means every odd sample count does.
            if (count % 3 != 0) tested_padding_cases = true;
        }
        check(all_ok, "every length round-trips byte for byte");
        check(tested_padding_cases, "the padded path was actually exercised");
    }

    // --- Extreme sample values must survive -----------------------------------------
    {
        const std::vector<int16_t> extremes = {-32768, 32767, 0, -1, 1, -32768, 32767};
        const std::string encoded = encode(extremes);
        std::vector<int16_t> decoded(extremes.size(), 0);
        const std::size_t produced =
            decode_chunk(encoded.c_str(), encoded.size(), decoded.data(), decoded.size());
        check(produced == extremes.size() &&
                  std::memcmp(extremes.data(), decoded.data(), extremes.size() * 2) == 0,
              "the full 16-bit range round-trips, including -32768");
    }

    // --- A full chunk must fit the declared buffer ----------------------------------
    {
        std::vector<int16_t> full(mic_capture::kChunkSamples, 1234);
        std::vector<char> buffer(mic_capture::kMaxEncodedChars);
        const std::size_t length =
            encode_chunk(full.data(), full.size(), buffer.data(), buffer.size());
        check(length > 0, "a full chunk encodes inside kMaxEncodedChars");
        check(length == 4 * ((mic_capture::kChunkSamples * 2 + 2) / 3),
              "the encoded length matches the base64 formula");
    }

    // --- Encoding must refuse rather than overflow ----------------------------------
    {
        std::vector<int16_t> pcm(16, 0);
        std::vector<char> too_small(8);
        check(encode_chunk(pcm.data(), pcm.size(), too_small.data(), too_small.size()) == 0,
              "encoding into a short buffer is refused, not truncated");
        check(encode_chunk(nullptr, 0, too_small.data(), too_small.size()) == 0,
              "a null input is refused");
    }

    // --- Decoding must refuse rather than truncate ----------------------------------
    {
        std::vector<int16_t> out(16, 0);
        check(decode_chunk("AAAA", 3, out.data(), out.size()) == 0,
              "an input whose length is not a multiple of four is rejected");
        check(decode_chunk("!!!!", 4, out.data(), out.size()) == 0,
              "invalid base64 characters are rejected");
        check(decode_chunk("AA=A", 4, out.data(), out.size()) == 0,
              "padding in a non-final position is rejected");
        check(decode_chunk("A===", 4, out.data(), out.size()) == 0,
              "padding in the first two slots is rejected");
        check(decode_chunk("AAAA", 4, out.data(), 1) == 0,
              "an output buffer too small for the payload is rejected");
        check(decode_chunk(nullptr, 4, out.data(), out.size()) == 0, "a null input is rejected");
        check(decode_chunk("AAAA", 4, nullptr, 0) == 0, "a null output is rejected");
    }

    // --- A truncated payload must not be silently accepted --------------------------
    {
        // Three bytes of payload encode to four characters; cutting a group off short
        // must not produce a short sample.
        std::vector<int16_t> pcm(2, 1000);
        const std::string encoded = encode(pcm);
        std::string truncated = encoded.substr(0, encoded.size() - 4);
        std::vector<int16_t> out(8, 0);
        check(decode_chunk(truncated.c_str(), truncated.size(), out.data(), out.size()) == 0,
              "a stream cut mid-chunk is rejected rather than decoded short");
    }

    // --- Round trip through a realistic chunk ---------------------------------------
    {
        std::mt19937 rng(11);
        std::normal_distribution<float> normal(0.0f, 2000.0f);
        std::vector<int16_t> pcm(mic_capture::kChunkSamples);
        for (auto& v : pcm) v = static_cast<int16_t>(normal(rng));

        const std::string encoded = encode(pcm);
        std::vector<int16_t> decoded(mic_capture::kChunkSamples, 0);
        const std::size_t produced =
            decode_chunk(encoded.c_str(), encoded.size(), decoded.data(), decoded.size());
        check(produced == mic_capture::kChunkSamples &&
                  std::memcmp(pcm.data(), decoded.data(), pcm.size() * 2) == 0,
              "a full noisy chunk round-trips exactly");
    }

    std::cout << "MicCapture: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
