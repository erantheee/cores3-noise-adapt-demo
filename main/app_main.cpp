#include <cstddef>
#include <cstdio>
#include <cstring>

#include "bsp/esp-bsp.h"
#include "controller.hpp"
#include "demo_ui.hpp"
#include "esp_codec_dev.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "latency_profiler.hpp"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mic_capture.hpp"
#include "mic_decimator.hpp"
#include "sdkconfig.h"

namespace {
constexpr uint32_t kSampleRate = 48000;
/// Samples per analysis frame. 2048 at 48 kHz is 42.7 ms and is what the 24-band
/// analysis was calibrated on.
constexpr std::size_t kFrameSamples = 2048;
/// Samples requested per hardware read. This is the quantisation step of the control
/// cadence, because the cadence test can only run once a read returns.
///
/// It used to equal kFrameSamples, which put the measured loop period at 170 ms against the
/// 150 ms target of that revision: the deadline is always crossed part-way through a read, so
/// the period overshoots by half a read on average, and half of 42.7 ms is 21 ms. Measured on
/// device, 150 + 21 = 171 ms, which is exactly what the report showed. A smaller block cuts the
/// overshoot proportionally; 512 samples is 10.7 ms, so half a read costs about 5 ms -- the
/// overshoot shrank, it did not disappear, which is why the measured period stays a hair above
/// whatever target it is given. That target is kControlPeriodMs below, and it is no longer
/// 150 ms.
constexpr std::size_t kReadBlockSamples = 512;
/// Largest analysis frame the accumulator can hold.
constexpr std::size_t kFrameSamplesMax = kFrameSamples + kReadBlockSamples;
/// What the control loop waits for between decisions. It was 150 ms; commit 4308366 brought it
/// to 100 ms, and the reasoning above is unaffected -- only the number the overshoot is measured
/// against.
constexpr float kControlPeriodMs = 100.0f;
/// During capture the analysis cadence is relaxed. The capture path costs a decimation
/// plus a base64 encode and a console write per chunk, so the two must not both run flat
/// out. 250 ms still resolves a change of environment, and it keeps the instrumentation
/// modest: the probe plus the analysis of one 2048-sample frame costs on the order of
/// 10-20 ms, which is under 10% of 250 ms. On device the analysis itself measures 7.03 ms p50
/// (see README), so 10-20 ms is the pessimistic end of that estimate, not the observed one.
constexpr float kCapturePeriodMs = 1000.0f;
/// The USB console measured on the real CoreS3 tops out near 11 kB/s despite accepting a
/// higher host baud setting. 16-bit/16-kHz PCM becomes ~43 kB/s after base64, so it can only
/// deliver one quarter of real time. Average each four 16-kHz samples for a 4-kHz proof-of-
/// concept speech relay; this is intentionally telephone-like, but continuous and honest.
constexpr uint32_t kRelayRate = capture::kOutputRate / 4;
constexpr std::size_t kRelayDownsample = capture::kOutputRate / kRelayRate;
static_assert(kRelayRate == 4000 && kRelayDownsample == 4);
/// 320 samples at 4 kHz is an 80 ms packet. The larger packet keeps JSON/base64 framing below
/// the remaining console budget while still staying usable for the relay experiment.
constexpr std::size_t kCaptureChunkSamples = 320;
/// How often the latency breakdown is reported. 10 s is frequent enough to watch a
/// rehearsal drift and rare enough that the report itself is not noise in the log.
constexpr int64_t kLatencyReportPeriodUs = 10 * 1000 * 1000;
const char* kTag = "cores3_noise_adapt";
MacAudioRoute g_mac_route;

/// Emit one frame of EarbudCommand v2.
///
/// Two deliberate changes from the previous revision:
///   * `noise_reduction_db` is gone. This product's actions are gain and tuning; the field
///     was computed, printed and shown on the device screen while nothing consumed it.
///   * `eq_gain_db` is now transmitted. It was always computed (24 per-band gains) and
///     applied to the command, but never sent -- so the host could not show the tuning at
///     all, and the panel had to infer its shape from the scalar `speech_gain_db`.
///
/// Band gains are printed with one decimal on purpose: they only ever hold 0 or the same
/// scalar speech gain, and one decimal keeps a frame near 250 bytes.
void emit_command_json(const EarbudCommand& command) {
    std::printf("{\"type\":\"earbud_command\",\"schema\":%lu,\"sequence\":%lu,"
                "\"timestamp_ms\":%lu,\"speech_gain_db\":%.2f,"
                "\"external_voice_gain_db\":%.2f,\"media_percent\":%u,"
                "\"state\":\"%s\",\"environment\":\"%s\",\"mode\":\"%s\","
                "\"conversation\":\"%s\",\"confidence\":%.2f,"
                "\"execution\":\"decision_only\",\"link\":\"simulated\","
                "\"eq_gain_db\":[",
                static_cast<unsigned long>(command.schema_version),
                static_cast<unsigned long>(command.sequence),
                static_cast<unsigned long>(command.timestamp_ms),
                command.speech_gain_db, command.external_voice_gain_db,
                command.media_percent,
                adapt_state_name(command.state), environment_name(command.environment),
                tuning_mode_name(command.tuning_mode),
                conversation_state_name(command.conversation),
                command.confidence);
    for (std::size_t band = 0; band < kBandCount; ++band) {
        std::printf("%s%.1f", band == 0 ? "" : ",", static_cast<double>(command.eq_gain_db[band]));
    }
    std::printf("],\"ambient_band_dbfs\":[");
    for (std::size_t band = 0; band < kBandCount; ++band) {
        std::printf("%s%.1f", band == 0 ? "" : ",",
                    static_cast<double>(command.ambient_band_dbfs[band]));
    }
    std::printf("]}\n");
}

/// Report the per-stage breakdown, then clear the window so the next report covers the
/// next interval. A rolling window that never resets would blend the whole session and
/// hide a regression that started an hour in.
void emit_latency_report(latency::LatencyProfiler& profiler,
                         const latency::ProfilerOverhead& overhead, uint32_t frames) {
    std::printf("{\"type\":\"latency\",\"frames\":%lu,\"total_p50_ms\":%.2f,"
                "\"total_p95_ms\":%.2f,\"overhead_mean_us\":%.2f,\"overhead_max_us\":%.2f,"
                "\"overhead_samples\":%lu,\"stages\":[",
                static_cast<unsigned long>(frames), profiler.total_p50_ms(),
                profiler.total_p95_ms(), static_cast<double>(overhead.mean_us()),
                static_cast<double>(overhead.max_us()),
                static_cast<unsigned long>(overhead.samples()));
    for (std::size_t i = 0; i < latency::kStageCount; ++i) {
        const auto stage = static_cast<latency::Stage>(i);
        const latency::StageStats stats = profiler.stats(stage);
        std::printf("%s{\"stage\":\"%s\",\"n\":%lu,\"p50_ms\":%.3f,\"p95_ms\":%.3f,"
                    "\"max_ms\":%.3f}",
                    i == 0 ? "" : ",", latency::stage_name(stage),
                    static_cast<unsigned long>(stats.samples),
                    static_cast<double>(stats.p50_ms), static_cast<double>(stats.p95_ms),
                    static_cast<double>(stats.max_ms));
    }
    std::printf("]}\n");
}

// ---------------------------------------------------------------------------------
// Capture over the serial console
//
// `rec start [seconds]` / `rec stop` stream the microphone as 4 kHz base64 chunks, one
// JSON object per line. The three accounts this project has to settle -- speaker-embedding
// separability, near-field signal-to-noise, and the judge thresholds -- all need audio as
// this device hears it. A laptop recording measures the laptop's acoustics instead.
//
// The real USB console measured about 11 kB/s. A 16-kHz stream needs ~43 kB/s after base64
// and therefore played only one quarter of real time. The relay stream is downsampled again;
// analysis remains at 16 kHz and is unaffected.
// ---------------------------------------------------------------------------------

/// Capture request from the console: -1 means "not capturing", 0 means "capture until
/// stopped", a positive value is a duration in seconds. The two zero-ish cases are
/// distinguished explicitly because conflating them made a duration of zero mean stop.
/// Written by the console poll and read by the audio task.
volatile int32_t g_record_seconds = -1;

constexpr std::size_t kConsoleLineMax = 96;
char g_console_line[kConsoleLineMax];
std::size_t g_console_fill = 0;

/// Read whatever the console has and act on complete lines. Returns true when a command
/// was handled, so the caller can act without polling twice.
bool poll_console_command() {
    bool handled = false;
    int next = std::getchar();
    while (next != EOF) {
        const char c = static_cast<char>(next);
        if (c == '\r' || c == '\n') {
            g_console_line[g_console_fill] = '\0';
            if (g_console_fill > 0) {
                int seconds = 0;
                if (std::strncmp(g_console_line, "rec start", 9) == 0) {
                    const int parsed = std::sscanf(g_console_line + 9, "%d", &seconds);
                    g_record_seconds = (parsed == 1 && seconds > 0) ? seconds : 0;
                    std::printf("{\"type\":\"capture\",\"state\":\"started\","
                                "\"seconds\":%ld,\"rate\":%lu}\n",
                                static_cast<long>(g_record_seconds),
                                static_cast<unsigned long>(capture::kOutputRate));
                    handled = true;
                } else if (std::strncmp(g_console_line, "rec stop", 8) == 0) {
                    g_record_seconds = -1;
                    std::printf("{\"type\":\"capture\",\"state\":\"stopped\"}\n");
                    handled = true;
                } else if (std::strncmp(g_console_line, "route active ", 13) == 0) {
                    std::snprintf(g_mac_route.device_name, sizeof(g_mac_route.device_name),
                                  "%.*s",
                                  static_cast<int>(sizeof(g_mac_route.device_name) - 1),
                                  g_console_line + 13);
                    g_mac_route.mac_connected = true;
                    g_mac_route.audio_output_active = true;
                    std::printf("{\"type\":\"route\",\"state\":\"active\","
                                "\"device\":\"%s\"}\n", g_mac_route.device_name);
                    handled = true;
                } else if (std::strncmp(g_console_line, "route inactive ", 15) == 0) {
                    std::snprintf(g_mac_route.device_name, sizeof(g_mac_route.device_name),
                                  "%.*s",
                                  static_cast<int>(sizeof(g_mac_route.device_name) - 1),
                                  g_console_line + 15);
                    g_mac_route.mac_connected = true;
                    g_mac_route.audio_output_active = false;
                    std::printf("{\"type\":\"route\",\"state\":\"inactive\","
                                "\"device\":\"%s\"}\n", g_mac_route.device_name);
                    handled = true;
                } else if (std::strcmp(g_console_line, "route offline") == 0) {
                    g_mac_route.mac_connected = false;
                    g_mac_route.audio_output_active = false;
                    std::printf("{\"type\":\"route\",\"state\":\"offline\"}\n");
                    handled = true;
                }
            }
            g_console_fill = 0;
        } else if (g_console_fill + 1 < kConsoleLineMax) {
            g_console_line[g_console_fill++] = c;
        } else {
            // An overlong line is discarded rather than truncated into something that
            // might parse as a valid command.
            g_console_fill = 0;
        }
        next = std::getchar();
    }
    return handled;
}

/// `encoded` must hold mic_capture::kMaxEncodedChars. Passed in rather than held as a
/// file-scope static: the buffer is only needed for the duration of the call, and a
/// module-level 2.7 KB array hides what the capture path actually costs.
void emit_capture_chunk(const int16_t* samples, std::size_t count, uint32_t sequence,
                        char* encoded) {
    const std::size_t length =
        mic_capture::encode_chunk(samples, count, encoded, mic_capture::kMaxEncodedChars);
    if (length == 0) {
        // Should be unreachable: the buffer is sized for the chunk size. Report rather
        // than emit a malformed line that the host would silently skip.
        std::printf("{\"type\":\"capture\",\"state\":\"encode_failed\",\"samples\":%lu}\n",
                    static_cast<unsigned long>(count));
        return;
    }
    std::printf("{\"type\":\"mic_chunk\",\"sequence\":%lu,\"timestamp_ms\":%lu,"
                "\"samples\":%lu,\"rate\":%lu,\"pcm\":\"%s\"}\n",
                static_cast<unsigned long>(sequence),
                static_cast<unsigned long>(esp_timer_get_time() / 1000),
                static_cast<unsigned long>(count),
                static_cast<unsigned long>(kRelayRate), encoded);
}

void processing_task(void*) {
    i2s_std_config_t audio_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(kSampleRate),
        .slot_cfg = I2S_STD_PHILIP_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                       I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = BSP_I2S_MCLK,
            .bclk = BSP_I2S_SCLK,
            .ws = BSP_I2S_LCLK,
            .dout = BSP_I2S_DOUT,
            .din = BSP_I2S_DSIN,
            .invert_flags = {},
        },
    };
    ESP_ERROR_CHECK(bsp_audio_init(&audio_config));
    esp_codec_dev_handle_t microphone = bsp_audio_codec_microphone_init();
    if (microphone == nullptr) {
        ESP_LOGE(kTag, "Microphone codec initialization failed");
        vTaskDelete(nullptr);
    }
    esp_codec_dev_sample_info_t format = {};
    format.sample_rate = kSampleRate;
    format.channel = 1;
    format.bits_per_sample = 16;
    ESP_ERROR_CHECK(esp_codec_dev_set_in_gain(microphone, 36.0f));
    ESP_ERROR_CHECK(esp_codec_dev_open(microphone, &format));

    // Staging for the codec read. Measured: the codec returns what the DMA has ready rather
    // than filling the requested block -- ~20 successful reads per second of 512 samples is
    // 10.2 k samples/s against the 48 k it is actually producing, so tying one block to one
    // read capped the recording at about a fifth of real time. Reads are decoupled from the
    // block size: take whatever arrives and hand a block on once one has accumulated.
    // Staging has two independent consumers: the analysis window, and -- during a recording --
    // the decimator. The read is the fast producer (one block per ~10 ms) while the decimator
    // drains what it can per pass, so staging needs real slack: at four blocks the buffer filled,
    // the top-up loop stopped reading, and the recording ran at 12766 of 16000 samples/s.
    // Eight blocks is 8 KB and takes that to the full rate. Capacity must be a whole number of
    // blocks, or the top-up loop leaves a remainder that never reaches the block threshold.
    constexpr std::size_t kStageCapacity = 8 * kReadBlockSamples;
    static_assert(kStageCapacity % kReadBlockSamples == 0,
                  "staging capacity must be a whole number of blocks");
    auto* stage = static_cast<int16_t*>(
        heap_caps_malloc(kStageCapacity * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    auto* samples = static_cast<int16_t*>(
        heap_caps_malloc(kFrameSamplesMax * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (samples == nullptr || stage == nullptr) {
        ESP_LOGE(kTag, "Audio buffer allocation failed");
        vTaskDelete(nullptr);
    }
    // Capacity for one read block's decimation, plus the phase carryover.
    //
    // `+ 2` is not padding, it is the fix for a silent total loss of the recording. The
    // decimator computes its worst case as (count + phase) / kDecimation and returns 0 --
    // writing nothing at all -- when the buffer is too small. 512 is not a multiple of 3, so
    // the phase carries over (512 % 3 = 2), and the worst case reaches 172 while `+ 1` sized
    // the buffer for 171. It therefore returned 0 on every call from that point on, for good:
    // the phase never advanced again, so the block was永远 discarded, one block in every one.
    //
    // Measured on device: a 10-second recording produced exactly one chunk of 170 samples,
    // with the capture diagnostic reporting `reads` climbing while `decimated` stayed at 170.
    constexpr std::size_t kDecimatedCapacity = kReadBlockSamples / capture::kDecimation + 2;
    static_assert(kDecimatedCapacity >=
                      (kReadBlockSamples + capture::kDecimation - 1) / capture::kDecimation + 1,
                  "the decimated buffer must cover a block plus the phase carryover");
    auto* decimated = static_cast<int16_t*>(
        heap_caps_malloc(kDecimatedCapacity * sizeof(int16_t),
                         MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    auto* pending = static_cast<int16_t*>(
        heap_caps_malloc(kCaptureChunkSamples * sizeof(int16_t),
                         MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    auto* encode_buffer =
        static_cast<char*>(heap_caps_malloc(mic_capture::kMaxEncodedChars, MALLOC_CAP_8BIT));
    if (decimated == nullptr || pending == nullptr || encode_buffer == nullptr) {
        ESP_LOGE(kTag, "Capture buffer allocation failed");
        vTaskDelete(nullptr);
    }

    NoiseAnalyzer analyzer;
    AdaptController controller;
    latency::LatencyProfiler profiler;
    latency::ProfilerOverhead overhead;
    int64_t next_latency_report_us = esp_timer_get_time() + kLatencyReportPeriodUs;
    uint32_t analysed_frames = 0;
    capture::Decimator decimator;
    uint32_t capture_sequence = 0;
    std::size_t pending_samples = 0;
    int32_t relay_sum = 0;
    std::size_t relay_sum_samples = 0;
    int64_t capture_deadline_us = 0;  // 0 = capture until stopped
    int64_t last_control_us = esp_timer_get_time();
    // Capture-path counters for the periodic diagnostic below. The first recording attempt
    // produced one chunk of 170 samples in 15 seconds and the log said nothing about why,
    // so the next failure has to be readable off the wire instead of re-derived.
    uint32_t capture_reads = 0;
    uint32_t capture_read_failures = 0;
    uint32_t capture_decimated_samples = 0;
    uint32_t capture_no_data_us = 0;
    uint32_t capture_ok_reads = 0;
    uint32_t capture_blocks_to_decimator = 0;
    uint32_t capture_samples_to_decimator = 0;
    uint32_t capture_max_filled = 0;
    uint32_t capture_max_staged = 0;
    uint32_t capture_ok_read_us = 0;
    uint32_t capture_analyse_us = 0;
    uint32_t capture_last_produced = 0;
    int64_t next_capture_diag_us = 0;

    std::size_t filled = 0;  // samples currently in the analysis window
    std::size_t staged = 0;  // samples waiting in the staging buffer
    while (true) {
        // esp_codec_dev_read returns a *status code*, not a byte count:
        //   ESP_CODEC_DEV_OK (0) on success, negative ESP_CODEC_DEV_* on failure.
        // Reading it as a count is what silenced this firmware once already -- the check
        // `bytes > 0` was false on every successful read, so the window never filled and no
        // frame was ever emitted.
        //
        // On success the block was filled in full (the driver reads exactly `len`), so the
        // window advances by the whole block. That is the original behaviour and it is right;
        // what was wrong before was feeding the decimator a full block *unconditionally*,
        // which is handled at the capture call site below.
        // One read per pass, and never more than fits. Two rules, both learned from freezes:
        //
        //   * The loop must not depend on a consumer draining a buffer before it can finish an
        //     iteration. Staging exists only for the recording's whole-block decimation and has
        //     no consumer at all while idle; a `while (staged < kStageCapacity)` top-up therefore
        //     spins forever once staging fills, `now_us` stops advancing, and the device goes
        //     silent on the wire. That is exactly what the 8-block version did.
        //   * Reading the whole staging buffer in one call is the shape that returns almost no
        //     data (the codec hands back what the DMA has ready), so ask for at most one block.
        esp_err_t read_result = ESP_OK;
        std::size_t got = 0;
        if (staged < kStageCapacity && filled + kReadBlockSamples <= kFrameSamplesMax) {
            const std::size_t want = std::min(kReadBlockSamples, kStageCapacity - staged);
            const int64_t read_started_us = esp_timer_get_time();
            read_result = esp_codec_dev_read(microphone, stage + staged,
                                             static_cast<int>(want * sizeof(int16_t)));
            const uint32_t read_us =
                static_cast<uint32_t>(esp_timer_get_time() - read_started_us);
            if (read_result != ESP_OK) {
                // Empty read: count its own bucket. Keeping one total and one success total
                // invited a uint32 subtraction that underflowed to 4294966 ms.
                if (g_record_seconds >= 0) capture_no_data_us += read_us;
            } else {
                staged += want;
                ++capture_reads;
                if (g_record_seconds >= 0) {
                    ++capture_ok_reads;
                    capture_ok_read_us += read_us;
                }
            }
        }

        // Top up the analysis window from staging. At most two blocks per pass: 2048 samples
        // every 150 ms is all the 24-band analysis needs. Staging keeps its remainder --
        // clearing it here is what made four blocks in every five disappear, because the window
        // reaches its high-water mark during a recording while audio keeps arriving at 48 kHz.
        //
        // During a recording this is skipped entirely. Measured: 471 blocks read against 379
        // reaching the decimator, with the staging high-water mark at 512 of 4096 -- so staging
        // was never full, and the missing blocks were the ones the analysis window took. A
        // recording is the scarce consumer here; the analysis can run on whatever it already has,
        // because the capture cadence has already been relaxed to 250 ms for exactly this reason.
        const bool capturing = g_record_seconds >= 0;
        for (int block = 0; block < 2 && !capturing; ++block) {
            if (staged < kReadBlockSamples) break;
            if (filled + kReadBlockSamples > kFrameSamplesMax) break;
            for (std::size_t i = 0; i < kReadBlockSamples; ++i) {
                samples[filled + i] = stage[i];
            }
            filled += kReadBlockSamples;
            got = kReadBlockSamples;
            const std::size_t remaining = staged - kReadBlockSamples;
            for (std::size_t i = 0; i < remaining; ++i) {
                stage[i] = stage[kReadBlockSamples + i];
            }
            staged = remaining;
        }
        // If the window cannot take another block, the staged remainder would be stale by the
        // time the window drains: drop it rather than let it splice old audio into the next one.
        // Only drop the remainder when the window is genuinely near full; at 2048+ of 2560
        // there is still room for another block, and clearing there would throw away audio
        // on every pass.
        if (filled > capture_max_filled) capture_max_filled = static_cast<uint32_t>(filled);
        if (g_record_seconds >= 0 && staged > capture_max_staged) {
            // If this ever reaches kStageCapacity, the reader is stalled behind a full staging
            // buffer again -- that is the shape of the 20% shortfall, so keep it visible.
            capture_max_staged = static_cast<uint32_t>(staged);
        }
        const int64_t now_us = esp_timer_get_time();

        if (poll_console_command()) {
            const int32_t requested = g_record_seconds;
            if (requested >= 0) {
                // Restart cleanly: stale filter state would put a step in the recording.
                decimator.reset();
                capture_sequence = 0;
                pending_samples = 0;
                relay_sum = 0;
                relay_sum_samples = 0;
                capture_deadline_us =
                    requested > 0 ? now_us + static_cast<int64_t>(requested) * 1000000 : 0;
                capture_reads = 0;
                capture_read_failures = 0;
                capture_decimated_samples = 0;
                capture_no_data_us = 0;
                capture_ok_reads = 0;
                capture_blocks_to_decimator = 0;
                capture_samples_to_decimator = 0;
                capture_max_filled = 0;
                capture_max_staged = 0;
                capture_ok_read_us = 0;
                capture_analyse_us = 0;
                capture_last_produced = 0;
                next_capture_diag_us = now_us + 5 * 1000 * 1000;
            }
        }
        if (g_record_seconds >= 0 && got == 0) ++capture_read_failures;

        // Capture runs on EVERY frame, deliberately outside the analysis cadence gate
        // below. Sitting behind that gate was a real bug: during capture the analysis
        // period is 300 ms while frames arrive every ~43 ms, so six frames in seven were
        // discarded and the recording came out with periodic holes.
        // Capture runs on every pass, deliberately outside the analysis cadence gate, and it
        // feeds the decimator straight from staging rather than from the analysis window.
        //
        // Routing it through the window tied the recording's throughput to the analysis's
        // high-water mark: measured on device, 953 blocks read in 10 s (487936 samples, the full
        // 48 kHz) against only 196 blocks reaching the decimator. Decimation needs whole blocks
        // and no window state, so it consumes staging directly and staging keeps its remainder.
        if (capturing) {
            while (staged >= kReadBlockSamples) {
                // Relay and environment control consume the same microphone stream. Capture
                // used to starve analysis, so enabling relay silently froze VAD/EQ decisions.
                if (filled + kReadBlockSamples <= kFrameSamplesMax) {
                    for (std::size_t i = 0; i < kReadBlockSamples; ++i) {
                        samples[filled + i] = stage[i];
                    }
                    filled += kReadBlockSamples;
                    if (filled > capture_max_filled) {
                        capture_max_filled = static_cast<uint32_t>(filled);
                    }
                }
                const std::size_t produced =
                    decimator.process(stage, kReadBlockSamples, decimated, kDecimatedCapacity);
                ++capture_blocks_to_decimator;
                capture_samples_to_decimator += static_cast<uint32_t>(kReadBlockSamples);
                capture_decimated_samples += static_cast<uint32_t>(produced);
                capture_last_produced = static_cast<uint32_t>(produced);
                const std::size_t remaining = staged - kReadBlockSamples;
                for (std::size_t i = 0; i < remaining; ++i) {
                    stage[i] = stage[kReadBlockSamples + i];
                }
                staged = remaining;
                for (std::size_t i = 0; i < produced; ++i) {
                    relay_sum += decimated[i];
                    ++relay_sum_samples;
                    if (relay_sum_samples < kRelayDownsample) continue;
                    pending[pending_samples++] =
                        static_cast<int16_t>(relay_sum / static_cast<int32_t>(relay_sum_samples));
                    relay_sum = 0;
                    relay_sum_samples = 0;
                    if (pending_samples == kCaptureChunkSamples) {
                        emit_capture_chunk(pending, pending_samples, ++capture_sequence,
                                           encode_buffer);
                        pending_samples = 0;
                    }
                }
            }
        }

        // Diagnostics and the recording deadline are outside the block loop on purpose: a
        // recording that produces no complete block still has to report why, and still has to
        // end -- the previous shape only checked the deadline when a block had been produced.
        if (g_record_seconds >= 0) {
            if (now_us >= next_capture_diag_us) {
                next_capture_diag_us = now_us + 5 * 1000 * 1000;
                // `no_data` are reads that returned while the DMA had nothing ready; they are
                // expected and are not errors. `read_ms` is the total time spent inside the
                // blocking read, which is what tells a data-rate problem apart from a
                // loop-iteration problem.
                std::printf("{\"type\":\"capture\",\"state\":\"progress\","
                            "\"reads\":%lu,\"no_data\":%lu,"
                            "\"ok_reads\":%lu,\"ok_read_ms\":%lu,\"no_data_ms\":%lu,"
                            "\"analyse_ms\":%lu,\"to_decimator\":%lu,\"to_decimator_samples\":%lu,"
                            "\"max_filled\":%lu,\"max_staged\":%lu,\"stage_cap\":%lu,"
                            "\"decimated\":%lu,\"last_produced\":%lu,"
                            "\"pending\":%lu,\"chunks\":%lu}\n",
                            static_cast<unsigned long>(capture_reads),
                            static_cast<unsigned long>(capture_read_failures),
                            static_cast<unsigned long>(capture_ok_reads),
                            static_cast<unsigned long>(capture_ok_read_us / 1000),
                            static_cast<unsigned long>(capture_no_data_us / 1000),
                            static_cast<unsigned long>(capture_analyse_us / 1000),
                            static_cast<unsigned long>(capture_blocks_to_decimator),
                            static_cast<unsigned long>(capture_samples_to_decimator),
                            static_cast<unsigned long>(capture_max_filled),
                            static_cast<unsigned long>(capture_max_staged),
                            static_cast<unsigned long>(kStageCapacity),
                            static_cast<unsigned long>(capture_decimated_samples),
                            static_cast<unsigned long>(capture_last_produced),
                            static_cast<unsigned long>(pending_samples),
                            static_cast<unsigned long>(capture_sequence));
            }
            if (capture_deadline_us != 0 && now_us >= capture_deadline_us) {
                if (pending_samples > 0) {
                    emit_capture_chunk(pending, pending_samples, ++capture_sequence,
                                       encode_buffer);
                    pending_samples = 0;
                }
                g_record_seconds = -1;
                capture_deadline_us = 0;
                std::printf("{\"type\":\"capture\",\"state\":\"finished\","
                            "\"chunks\":%lu}\n",
                            static_cast<unsigned long>(capture_sequence));
            }
        }

        const float period_ms = (g_record_seconds >= 0) ? kCapturePeriodMs : kControlPeriodMs;
        const bool due = (now_us - last_control_us) >=
                         static_cast<int64_t>(period_ms * 1000.0f);
        // Both conditions are required: the cadence decides when a decision is wanted, the
        // window decides whether there is enough audio to decide on. Waiting for a full
        // window rather than a partial one keeps the 24-band analysis operating on the
        // frame length it was calibrated on.
        if (!due || filled < kFrameSamples) continue;

        // Overhead probe. The previous version read the clock once before the analysis and
        // again after the whole frame, which meant "overhead" was really the analysis time:
        // it reported 57 ms while the analysis itself reported 57 ms. What this measures is
        // the cost of the two clock reads, which is the honest figure for instrumentation.
        const int64_t probe_us = esp_timer_get_time();
        const float capture_ms = static_cast<float>(probe_us - last_control_us) / 1000.0f;

        const int64_t analyse_started_us = esp_timer_get_time();
        AnalysisFrame frame = analyzer.analyze(samples, filled,
            static_cast<uint32_t>(now_us / 1000));
        if (g_record_seconds >= 0) {
            capture_analyse_us += static_cast<uint32_t>(esp_timer_get_time() - analyse_started_us);
        }
        // An empty read is not a broken microphone. `read_result != ESP_OK` here means the DMA
        // had nothing ready this pass, which happens routinely while the loop is keeping up;
        // treating it as a fault raised MIC FAULT eight times in the first five seconds after
        // boot, zeroing the gain each time. The read error is still counted (`no_data`) but it no
        // longer claims the microphone is broken. A microphone that is genuinely dead produces
        // out-of-range samples, which the clipping check in the analyser already catches.
        (void)read_result;
        const int64_t analyse_done_us = esp_timer_get_time();

        const float elapsed_ms = static_cast<float>(now_us - last_control_us) / 1000.0f;
        EarbudCommand command = controller.update(frame, elapsed_ms);
        const int64_t decide_done_us = esp_timer_get_time();

        const float analyse_ms = static_cast<float>(analyse_done_us - analyse_started_us) / 1000.0f;
        const float decide_ms = static_cast<float>(decide_done_us - analyse_done_us) / 1000.0f;
        const float processing_ms = static_cast<float>(decide_done_us - analyse_started_us) / 1000.0f;

        // Interval between analyses, i.e. how often the decision is refreshed. See the
        // note on Stage::capture: this is not the hardware buffering delay alone.
        profiler.record(latency::Stage::capture, capture_ms);
        profiler.record(latency::Stage::analyse, analyse_ms);
        // The embedding stage stays at zero until speaker embeddings exist on the device.
        // It is reported anyway so the column is present and its absence is visible rather
        // than being mistaken for "costs nothing".
        profiler.record(latency::Stage::embed, 0.0f);
        profiler.record(latency::Stage::decide, decide_ms);
        // Instrumentation cost: two esp_timer_get_time calls back to back. The previous
        // two revisions of this line both reported the analysis instead of the probes --
        // first 57 ms, then 7.1 ms -- because the closing read sat after the work rather
        // than beside the opening read. Both times the figure looked plausible enough to
        // quote. Keeping the two reads adjacent is the whole point of the measurement, so
        // do not move them apart to "also time something else".
        const int64_t probe_start_us = esp_timer_get_time();
        const int64_t probe_end_us = esp_timer_get_time();
        overhead.record(static_cast<float>(probe_end_us - probe_start_us));
        ++analysed_frames;

        emit_command_json(command);

        if (bsp_display_lock(100)) {
            demo_ui_update(frame, command, g_mac_route, processing_ms,
                           esp_get_free_heap_size());
            bsp_display_unlock();
        }

        if (esp_timer_get_time() >= next_latency_report_us) {
            emit_latency_report(profiler, overhead, analysed_frames);
            profiler.reset();
            next_latency_report_us = esp_timer_get_time() + kLatencyReportPeriodUs;
        }

        last_control_us = now_us;
        filled = 0;
    }
}
}  // namespace

extern "C" void app_main(void) {
    ESP_LOGI(kTag, "Starting earbud-case-shaped edge audio controller");
    ESP_LOGI(kTag, "No autoplay. Built-in speaker remains silent.");
    ESP_LOGI(kTag, "Send 'rec start [seconds]' or 'rec stop' to capture the microphone.");
    bsp_display_start();
    bsp_display_lock(0);
    demo_ui_create();
    bsp_display_unlock();
    bsp_display_backlight_on();
    xTaskCreatePinnedToCore(processing_task, "audio_control", 12288, nullptr, 6, nullptr, 1);
}
