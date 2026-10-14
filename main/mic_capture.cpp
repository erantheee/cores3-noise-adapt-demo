#include "mic_capture.hpp"

namespace mic_capture {
namespace {
constexpr char kAlphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Reverse lookup, built once. 0xFF marks an invalid character.
struct DecodeTable {
    uint8_t value[256];
    constexpr DecodeTable() : value{} {
        for (int i = 0; i < 256; ++i) value[i] = 0xFF;
        for (int i = 0; i < 64; ++i) value[static_cast<uint8_t>(kAlphabet[i])] = static_cast<uint8_t>(i);
    }
};
constexpr DecodeTable kDecode{};
}  // namespace

std::size_t encode_chunk(const int16_t* samples, std::size_t count, char* out,
                         std::size_t out_size) {
    if (samples == nullptr || out == nullptr) return 0;
    // Every three input bytes become four output characters, and the string is terminated.
    const std::size_t needed = 4 * ((count * 2 + 2) / 3) + 1;
    if (out_size < needed) return 0;

    const auto* bytes = reinterpret_cast<const uint8_t*>(samples);
    const std::size_t byte_count = count * 2;
    std::size_t written = 0;
    std::size_t i = 0;
    for (; i + 2 < byte_count; i += 3) {
        const uint32_t triple = (static_cast<uint32_t>(bytes[i]) << 16) |
                                (static_cast<uint32_t>(bytes[i + 1]) << 8) |
                                static_cast<uint32_t>(bytes[i + 2]);
        out[written++] = kAlphabet[(triple >> 18) & 0x3F];
        out[written++] = kAlphabet[(triple >> 12) & 0x3F];
        out[written++] = kAlphabet[(triple >> 6) & 0x3F];
        out[written++] = kAlphabet[triple & 0x3F];
    }
    const std::size_t remaining = byte_count - i;
    if (remaining > 0) {
        uint32_t triple = static_cast<uint32_t>(bytes[i]) << 16;
        if (remaining == 2) triple |= static_cast<uint32_t>(bytes[i + 1]) << 8;
        out[written++] = kAlphabet[(triple >> 18) & 0x3F];
        out[written++] = kAlphabet[(triple >> 12) & 0x3F];
        out[written++] = remaining == 2 ? kAlphabet[(triple >> 6) & 0x3F] : '=';
        out[written++] = '=';
    }
    out[written] = '\0';
    return written;
}

std::size_t decode_chunk(const char* encoded, std::size_t encoded_length, int16_t* out,
                         std::size_t out_capacity) {
    if (encoded == nullptr || out == nullptr) return 0;
    if (encoded_length % 4 != 0) return 0;

    const std::size_t byte_capacity = out_capacity * 2;
    auto* bytes = reinterpret_cast<uint8_t*>(out);
    std::size_t produced = 0;

    for (std::size_t i = 0; i < encoded_length; i += 4) {
        uint8_t quad[4];
        int padding = 0;
        for (int k = 0; k < 4; ++k) {
            const char c = encoded[i + static_cast<std::size_t>(k)];
            if (c == '=') {
                // Padding is only legal in the last group and only in the last two slots.
                if (i + 4 != encoded_length || k < 2) return 0;
                quad[k] = 0;
                ++padding;
                continue;
            }
            if (padding > 0) return 0;  // a data character after padding
            const uint8_t decoded = kDecode.value[static_cast<uint8_t>(c)];
            if (decoded == 0xFF) return 0;
            quad[k] = decoded;
        }
        const uint32_t triple = (static_cast<uint32_t>(quad[0]) << 18) |
                                (static_cast<uint32_t>(quad[1]) << 12) |
                                (static_cast<uint32_t>(quad[2]) << 6) |
                                static_cast<uint32_t>(quad[3]);
        const int produced_here = 3 - padding;
        if (produced + static_cast<std::size_t>(produced_here) > byte_capacity) return 0;
        bytes[produced++] = static_cast<uint8_t>((triple >> 16) & 0xFF);
        if (produced_here > 1) bytes[produced++] = static_cast<uint8_t>((triple >> 8) & 0xFF);
        if (produced_here > 2) bytes[produced++] = static_cast<uint8_t>(triple & 0xFF);
    }

    if (produced % 2 != 0) return 0;  // a partial sample means the stream is broken
    return produced / 2;
}

}  // namespace mic_capture
