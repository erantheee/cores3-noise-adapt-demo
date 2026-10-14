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
/// It used to equal kFrameSamples, which put the measured loop period at 170 ms against a
/// 150 ms target: the deadline is always crossed part-way through a read, so the period
/// overshoots by half a read on average, and half of 42.7 ms is 21 ms. Measured on device,
/// 150 + 21 = 171 ms, which is exactly what the report showed. A smaller block cuts the
/// overshoot proportionally; 512 samples is 10.7 ms, so the period lands near 155 ms.
constexpr std::size_t kReadBlockSamples = 512;
/// Largest analysis frame the accumulator can hold.
constexpr std::size_t kFrameSamplesMax = kFrameSamples + kReadBlockSamples;
constexpr float kControlPeriodMs = 150.0f;
/// During capture the analysis cadence is relaxed. The capture path costs a decimation
/// plus a base64 encode and a console write per chunk, so the two must not both run flat
/// out. 250 ms still resolves a change of environment, and it keeps the instrumentation
/// inside its 1%-of-budget check: the probe plus the analysis of one 2048-sample frame
/// costs on the order of 10-20 ms, which is under 8% of 250 ms rather than 13% of 150 ms.
constexpr float kCapturePeriodMs = 250.0f;
/// Samples accumulated before a chunk is sent: 1024 at 16 kHz is 64 ms, which is exactly
/// three 2048-sample input frames at the 3:1 decimation, so blocks line up with no
/// leftover. Choose a size that is not a multiple of the decimation ratio and the capture
/// would still be correct but carry a partial chunk across frames.
constexpr std::size_t kCaptureChunkSamples = 1024;
/// How often the latency breakdown is reported. 10 s is frequent enough to watch a
/// rehearsal drift and rare enough that the report itself is not noise in the log.
constexpr int64_t kLatencyReportPeriodUs = 10 * 1000 * 1000;
const char* kTag = "cores3_noise_adapt";

/// Emit one frame of EarbudCommand v1.
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
                "\"media_percent\":%u,\"state\":\"%s\",\"environment\":\"%s\","
                "\"confidence\":%.2f,\"link\":\"simulated\",\"eq_gain_db\":[",
                static_cast<unsigned long>(command.schema_version),
                static_cast<unsigned long>(command.sequence),
                static_cast<unsigned long>(command.timestamp_ms),
                command.speech_gain_db, command.media_percent,
                adapt_state_name(command.state), environment_name(command.environment),
                command.confidence);
    for (std::size_t band = 0; band < kBandCount; ++band) {
        std::printf("%s%.1f", band == 0 ? "" : ",", static_cast<double>(command.eq_gain_db[band]));
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
// `rec start [seconds]` / `rec stop` stream the microphone as 16 kHz base64 chunks, one
// JSON object per line. The three accounts this project has to settle -- speaker-embedding
// separability, near-field signal-to-noise, and the judge thresholds -- all need audio as
// this device hears it. A laptop recording measures the laptop's acoustics instead.
//
// The link budget is the reason for 16 kHz: 48 kHz mono base64 is about 128 kB/s against
// roughly 92 kB/s of usable console throughput, while 16 kHz takes 46%.
// ---------------------------------------------------------------------------------

/// Capture request from the console: -1 means "not capturing", 0 means "capture until
/// stopped", a positive value is a duration in seconds. The two zero-ish cases are
/// distinguished explicitly because conflating them made a duration of zero mean stop.
/// Written by the console poll and read by the audio task.
volatile int32_t g_record_seconds = -1;

constexpr std::size_t kConsoleLineMax = 64;
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
                static_cast<unsigned long>(capture::kOutputRate), encoded);
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

    auto* read_block = static_cast<int16_t*>(
        heap_caps_malloc(kReadBlockSamples * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    auto* samples = static_cast<int16_t*>(
        heap_caps_malloc(kFrameSamplesMax * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (samples == nullptr || read_block == nullptr) {
        ESP_LOGE(kTag, "Audio buffer allocation failed");
        vTaskDelete(nullptr);
    }
    // 2048 input samples decimate to at most 683 output samples.
    auto* decimated = static_cast<int16_t*>(
        heap_caps_malloc((kReadBlockSamples / capture::kDecimation + 1) * sizeof(int16_t),
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
    int64_t capture_deadline_us = 0;  // 0 = capture until stopped
    int64_t last_control_us = esp_timer_get_time();

    std::size_t filled = 0;  // samples currently in the analysis window
    while (true) {
        esp_err_t read_result = ESP_OK;
        if (filled + kReadBlockSamples <= kFrameSamplesMax) {
            read_result = esp_codec_dev_read(microphone, read_block,
                                             kReadBlockSamples * sizeof(int16_t));
            for (std::size_t i = 0; i < kReadBlockSamples; ++i) {
                samples[filled + i] = read_block[i];
            }
            filled += kReadBlockSamples;
        }
        const int64_t now_us = esp_timer_get_time();

        if (poll_console_command()) {
            const int32_t requested = g_record_seconds;
            if (requested >= 0) {
                // Restart cleanly: stale filter state would put a step in the recording.
                decimator.reset();
                capture_sequence = 0;
                pending_samples = 0;
                capture_deadline_us =
                    requested > 0 ? now_us + static_cast<int64_t>(requested) * 1000000 : 0;
            }
        }

        // Capture runs on EVERY frame, deliberately outside the analysis cadence gate
        // below. Sitting behind that gate was a real bug: during capture the analysis
        // period is 300 ms while frames arrive every ~43 ms, so six frames in seven were
        // discarded and the recording came out with periodic holes.
        if (g_record_seconds >= 0) {
            const std::size_t produced = decimator.process(read_block, kReadBlockSamples,
                                                           decimated,
                                                           kReadBlockSamples / capture::kDecimation + 1);
            for (std::size_t i = 0; i < produced; ++i) {
                pending[pending_samples++] = decimated[i];
                if (pending_samples == kCaptureChunkSamples) {
                    emit_capture_chunk(pending, pending_samples, ++capture_sequence,
                                       encode_buffer);
                    pending_samples = 0;
                }
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
        if (read_result != ESP_OK) frame.mic_valid = false;
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
            demo_ui_update(frame, command, processing_ms, esp_get_free_heap_size());
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
