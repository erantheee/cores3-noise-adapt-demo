#pragma once

#include <cstddef>
#include <cstdint>

/// Microphone capture streamed over the serial console.
///
/// Why this exists: the three accounts this project has to settle -- speaker-embedding
/// separability, near-field signal-to-noise, and the judge thresholds -- all need audio as
/// the *device* hears it. Audio recorded on a laptop measures the laptop's acoustics and
/// says nothing about the device, so there has to be a way to get the device's own capture
/// out.
///
/// Why base64 text rather than raw frames: the firmware already emits one JSON line per
/// control period on the same console. Keeping the capture in the same shape means one
/// transport, no framing negotiation, and a host script that is a few lines long. Raw
/// binary would be slightly smaller and considerably harder to debug when it goes wrong.
///
/// 16 kHz is the default because that is what speaker-embedding models are trained on and
/// what the standard speech-band analysis covers. 48 kHz is available when the caller needs
/// the full band, at three times the link budget.
namespace mic_capture {

/// Samples per transmitted chunk. 1024 samples is 128 ms at 8 kHz and 21 ms at 48 kHz,
/// and encodes to 2731 base64 characters, which fits the console comfortably.
constexpr std::size_t kChunkSamples = 1024;

/// Encoded chunk plus its terminator. Must be derived from the sample count in *bytes*
/// (two per sample) and must include the NUL, or encode_chunk refuses to write and the
/// capture silently produces nothing.
constexpr std::size_t kMaxEncodedChars = 4 * ((kChunkSamples * 2 + 2) / 3) + 1;

/// Base64-encode PCM samples. Writes a NUL-terminated string and returns its length,
/// or 0 when the output buffer is too small.
std::size_t encode_chunk(const int16_t* samples, std::size_t count, char* out,
                         std::size_t out_size);

/// Unpack little-endian PCM from the base64 payload of a capture line.
///
/// Returns the number of samples written, or 0 when the input is not valid base64 or the
/// output buffer is too small. Decoding must never silently truncate: a half frame would
/// shift every later sample and corrupt the recording in a way that is hard to notice.
std::size_t decode_chunk(const char* encoded, std::size_t encoded_length, int16_t* out,
                         std::size_t out_capacity);

}  // namespace mic_capture
