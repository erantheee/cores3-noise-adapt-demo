#include <cmath>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <new>

#include "bone_voice.hpp"
#include "bsp/esp-bsp.h"
#include "context_window.hpp"
#include "controller.hpp"
#include "demo_ui.hpp"
#include "dual_mic.hpp"
#include "esp_codec_dev.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "latency_profiler.hpp"
#include "speech_gate_model.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "imu.hpp"
#include "mic_capture.hpp"
#include "mic_decimator.hpp"
#include "sdkconfig.h"
#include "sound_event.hpp"

namespace {
constexpr uint32_t kSampleRate = 48000;

/// IMU 采样。BMI270 配成 100 Hz，任务每 5 ms 轮询一次数据就绪位（与 ear-context 实测一致）。
/// 100 Hz 下 Nyquist 是 50 Hz：看不到骨传导语音振动的频带本身，但**包络**（音节率 2–8 Hz）
/// 完整可观测，而判据用的就是包络相关性（见 main/bone_voice.hpp）。
/// 自身语音证据策略（用户 2026-10-16 决定：**演示用近场代理**）。
///
/// 为什么是演示模式：骨传导判据已被本仓库自己的真机数据否定（docs/apple-conversation-awareness.md
/// 第 13 节，与既有 AUC 0.0508 一致），而 fail-closed 下演示看不到任何行为。
/// 代价写在文档第 15 节：**旁人说话也会触发**，所以遥测与面板必须显示 `near_field`。
/// 换回产品默认只需把这一行改成 EvidencePolicy::bone_only。
constexpr EvidencePolicy kEvidencePolicy = EvidencePolicy::near_field_demo;

constexpr uint32_t kImuPollMs = 5;
/// 队列深度。60 拍（6 秒）的缓冲：控制任务偶尔被分析/推理拖慢时，IMU 采样不丢。
constexpr UBaseType_t kImuQueueDepth = 64;

QueueHandle_t g_imu_queue = nullptr;
volatile bool g_imu_ok = false;
volatile uint32_t g_imu_samples = 0;
volatile uint32_t g_imu_drops = 0;
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
constexpr float kCapturePeriodMs = 250.0f;
/// Relay at the analyser's 16-kHz speech rate. Once hardware capture was decoupled from the
/// full analysis window, the apparent 4:1 "serial bottleneck" disappeared: it had actually
/// been four reads per analysis period. 320 samples is a 20-ms packet, reducing packetisation
/// delay by 60 ms versus the temporary 4-kHz proof path while restoring speech clarity.
constexpr uint32_t kRelayRate = capture::kOutputRate;
constexpr std::size_t kCaptureChunkSamples = 320;
/// How often the latency breakdown is reported. 10 s is frequent enough to watch a
/// rehearsal drift and rare enough that the report itself is not noise in the log.
constexpr int64_t kLatencyReportPeriodUs = 10 * 1000 * 1000;
const char* kTag = "cores3_noise_adapt";
MacAudioRoute g_mac_route;

// ---- Mac 侧 WavLM 声纹判定（串口回传）----
// self/other 是**说话人判定**，不是目标人分类：self = 佩戴者自己，other = 别人。
// D28 step3 的语义：self → 不压媒体、不抬歌曲 EQ、不开环境声；other 与"没有判定"都按
// D16 的预判逻辑走。
//
// 判定超过 kVoiceprintHoldUs 没有新行就视为过期，过期后不再影响任何决策。这是对
// "worker 挂一次 / 上位机重启后对话被永久锁死"的修复：门控必须能自己退出，而不是
// 等设备重启。emit_command_json 也读这几个量做 v4 遥测，所以它们定义在它之前。
bool g_voiceprint_valid = false;
bool g_voiceprint_self = false;
float g_voiceprint_score = 0.0f;
int64_t g_voiceprint_us = 0;
/// 3 s 内没有新判定就当作不可用：既用于 self veto，也用于授权过期。
constexpr int64_t kVoiceprintHoldUs = 3000000;

/// Emit one frame of EarbudCommand v4.
///
/// v3 adds the own-voice evidence (`own_voice_confidence` / `own_voice_correlation` /
/// `own_voice_source`) plus `imu_ok`. Without it a device-side verification cannot tell
/// "bone evidence never arrived" apart from "evidence arrived and the state machine still
/// did not duck" -- and those two have completely different fixes.
///
/// v4 adds `ambient_open`: the anticipation state machine's "let reality in" intent. The
/// device cannot execute it itself; the host maps it to an AirPods listening-mode switch and
/// must report that execution separately from this intent.
///
/// Two deliberate changes from an earlier revision:
///   * `noise_reduction_db` is gone. This product's actions are gain and tuning; the field
///     was computed, printed and shown on the device screen while nothing consumed it.
///   * `eq_gain_db` is now transmitted. It was always computed (24 per-band gains) and
///     applied to the command, but never sent -- so the host could not show the tuning at
///     all, and the panel had to infer its shape from the scalar `speech_gain_db`.
///
/// Band gains are printed with one decimal on purpose: they only ever hold 0 or a small
/// per-band value from SONG_EQ, and one decimal keeps a frame near 250 bytes.
void emit_command_json(const EarbudCommand& command) {
    // 声纹门控遥测（v4 只增键）：valid 是"当前是否有新鲜判定"而不是"曾经收到过"，
    // age=-1 表示从未收到。把它报出来，现场才分得清"没人说话"与"声纹判定已过期/停用"。
    const int64_t voiceprint_age_us =
        g_voiceprint_us > 0 ? (esp_timer_get_time() - g_voiceprint_us) : -1;
    const VoiceprintDecision voiceprint =
        evaluate_voiceprint(g_voiceprint_valid, g_voiceprint_self, voiceprint_age_us,
                            kVoiceprintHoldUs);
    const long voiceprint_age_ms =
        voiceprint_age_us >= 0 ? static_cast<long>(voiceprint_age_us / 1000) : -1L;
    std::printf("{\"type\":\"earbud_command\",\"schema\":%lu,\"sequence\":%lu,"
                "\"timestamp_ms\":%lu,\"speech_gain_db\":%.2f,"
                "\"external_voice_gain_db\":%.2f,\"media_percent\":%u,"
                "\"ambient_open\":%s,"
                // 判据遥测：训练对话预判与排查误触发都要这些量（见 conversation-prediction.md）。
                "\"broadband_dbfs\":%.1f,\"voice_likelihood\":%.3f,"
                "\"vocal_ratio\":%.3f,\"spectral_flatness\":%.4f,"
                "\"vocal_peakiness\":%.3f,\"excited_vocal_bands\":%u,"
                // D47 双麦遥测（追加可选键，不涨 schema）：第二路麦电平、两路电平差、
                // 帧内相关系数。取数阶段只上报；mic2_valid=false 时报 -120/0/0。
                "\"mic2_broadband_dbfs\":%.1f,\"mic_diff_db\":%.2f,"
                "\"mic_coherence\":%.3f,\"mic2_valid\":%s,"
                "\"state\":\"%s\",\"environment\":\"%s\",\"noise_spectrum\":\"%s\",\"mode\":\"%s\","
                "\"conversation\":\"%s\",\"confidence\":%.2f,"
                "\"own_voice_confidence\":%.3f,\"own_voice_correlation\":%.3f,"
                "\"own_voice_vibration\":%.4f,\"imu_gravity\":%.3f,"
                "\"own_voice_source\":\"%s\","
                "\"imu_ok\":%s,\"imu_motion\":%s,\"imu_drops\":%lu,"
                "\"imu_samples\":%lu,\"own_voice_policy\":\"%s\","
                // 声纹门控遥测（v4 上追加的键，不涨 schema）：valid = 当前是否有新鲜判定，
                // self = 当前 verdict，age_ms = -1 表示从未收到。
                "\"voiceprint_valid\":%s,\"voiceprint_self\":%s,"
                "\"voiceprint_age_ms\":%ld,"
                "\"execution\":\"decision_only\",\"link\":\"simulated\","
                "\"eq_gain_db\":[",
                static_cast<unsigned long>(command.schema_version),
                static_cast<unsigned long>(command.sequence),
                static_cast<unsigned long>(command.timestamp_ms),
                command.speech_gain_db, command.external_voice_gain_db,
                command.media_percent,
                command.ambient_open ? "true" : "false",
                static_cast<double>(command.broadband_dbfs),
                static_cast<double>(command.voice_likelihood),
                static_cast<double>(command.vocal_ratio),
                static_cast<double>(command.spectral_flatness),
                static_cast<double>(command.vocal_peakiness),
                static_cast<unsigned>(command.excited_vocal_bands),
                static_cast<double>(command.mic2_broadband_dbfs),
                static_cast<double>(command.mic_diff_db),
                static_cast<double>(command.mic_coherence),
                command.mic2_valid ? "true" : "false",
                adapt_state_name(command.state), environment_name(command.environment),
                noise_spectrum_name(command.noise_spectrum),
                tuning_mode_name(command.tuning_mode),
                conversation_state_name(command.conversation),
                command.confidence,
                static_cast<double>(command.own_voice.confidence),
                static_cast<double>(command.own_voice.correlation),
                static_cast<double>(command.own_voice.vibration_rms),
                static_cast<double>(command.own_voice.gravity),
                command.own_voice.bone ? "bone"
                                       : (command.own_voice.proxy ? "near_field" : "none"),
                command.own_voice.imu_ok ? "true" : "false",
                command.own_voice.motion ? "true" : "false",
                static_cast<unsigned long>(g_imu_drops),
                static_cast<unsigned long>(g_imu_samples),
                evidence_policy_name(command.evidence_policy),
                voiceprint.active ? "true" : "false",
                g_voiceprint_self ? "true" : "false",
                voiceprint_age_ms);
    for (std::size_t band = 0; band < kBandCount; ++band) {
        std::printf("%s%.1f", band == 0 ? "" : ",", static_cast<double>(command.eq_gain_db[band]));
    }
    std::printf("],\"ambient_band_dbfs\":[");
    for (std::size_t band = 0; band < kBandCount; ++band) {
        std::printf("%s%.1f", band == 0 ? "" : ",",
                    static_cast<double>(command.ambient_band_dbfs[band]));
    }
    // 情境模型只上报，不参与任何决策。`"context_valid":false` 就是"窗口还没攒够 2 秒"，
    // 上位机要能把它与"模型说 unknown"分开。类别名与训练那 9 个字符串逐字相同：
    // 屏幕、串口、模型头文件共用 listening_model.h 的 kClassNames 这一份。
    // 声音事件判断同样只上报：`event_valid=false` 表示 1 s 窗还没攒满（或刚被撞击清空），
    // 上位机必须把这两种情况与"判断成 quiet"分开显示。
    std::printf("],\"context\":\"%s\",\"context_score\":%.3f,\"context_valid\":%s,"
                "\"context_us\":%ld,"
                // D46 混合环境识别（追加可选键，不涨 schema，先例 voiceprint_*）：
                // top2 = softmax 第二名；mixed = top2 ≥ 0.20。valid=false 时报占位
                // unknown/0.0/false，与 context 组同构。
                "\"context_top2\":\"%s\",\"context_top2_score\":%.3f,"
                "\"context_mixed\":%s,"
                "\"event\":\"%s\",\"event_valid\":%s,"
                "\"speech_gate\":%.3f,\"speech_gate_valid\":%s,\"speech_gate_us\":%ld}\n",
                command.context.class_name, static_cast<double>(command.context.score),
                command.context.valid ? "true" : "false",
                static_cast<long>(command.context.inference_us),
                command.context.valid
                    ? listening_model::kClassNames[command.context.top2_index]
                    : "unknown",
                static_cast<double>(command.context.top2_score),
                command.context.mixed ? "true" : "false",
                command.event.class_name,
                command.event.valid ? "true" : "false",
                static_cast<double>(command.speech_gate_score),
                command.speech_gate_valid ? "true" : "false",
                static_cast<long>(command.speech_gate_us));
}

/// Report the per-stage breakdown, then clear the window so the next report covers the
/// next interval. A rolling window that never resets would blend the whole session and
/// hide a regression that started an hour in.
///
/// The model's own percentiles ride along as top-level fields rather than as an eighth
/// `stages[]` entry. The stage list is the energy path ("capture → analyse → embed → decide
/// → transport → apply → render"), it is named in the host's `LOCAL_STAGES`, and it is
/// asserted in two host tests; a model forward pass is not a stage of that path. Adding a
/// field to a known message type is invisible to the host parser (it reads keys by name),
/// while adding a stage would have dragged the panel, the protocol layer and their tests in.
///
/// `stack_high_water` is this task's minimum free stack, in bytes, since boot (this port
/// declares `portSTACK_TYPE` as uint8_t, so FreeRTOS' "words" are bytes here). It is in the
/// report because this round's first build overflowed that stack and rebooted the device in a
/// loop, and nothing in the log said how thin the margin had been beforehand. A number that
/// only ever decreases is the cheapest way to keep the next addition honest.
void emit_latency_report(latency::LatencyProfiler& profiler,
                         const latency::ProfilerOverhead& overhead,
                         const context::ContextTiming& context_timing,
                         const context::ContextTiming& event_timing, uint32_t frames) {
    std::printf("{\"type\":\"latency\",\"frames\":%lu,\"total_p50_ms\":%.2f,"
                "\"total_p95_ms\":%.2f,\"overhead_mean_us\":%.2f,\"overhead_max_us\":%.2f,"
                "\"overhead_samples\":%lu,\"context_n\":%lu,\"context_p50_us\":%.0f,"
                "\"context_p95_us\":%.0f,\"context_max_us\":%.0f,"
                "\"event_n\":%lu,\"event_p50_us\":%.0f,\"event_p95_us\":%.0f,"
                "\"event_max_us\":%.0f,\"stack_high_water\":%lu,"
                "\"stages\":[",
                static_cast<unsigned long>(frames), profiler.total_p50_ms(),
                profiler.total_p95_ms(), static_cast<double>(overhead.mean_us()),
                static_cast<double>(overhead.max_us()),
                static_cast<unsigned long>(overhead.samples()),
                static_cast<unsigned long>(context_timing.samples()),
                static_cast<double>(context_timing.percentile(0.5f)),
                static_cast<double>(context_timing.percentile(0.95f)),
                static_cast<double>(context_timing.max_us()),
                static_cast<unsigned long>(event_timing.samples()),
                static_cast<double>(event_timing.percentile(0.5f)),
                static_cast<double>(event_timing.percentile(0.95f)),
                static_cast<double>(event_timing.max_us()),
                static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
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

/// 调试用：最近一次满窗的 92 维特征。控制台发 `vector` 时原样打印，
/// 用来核对端侧特征与训练侧 features.py 是否同口径（golden 只覆盖金标准样例）。
float g_latest_window_vector[context::kFeatureCount];
bool g_latest_window_vector_valid = false;
uint32_t g_latest_window_vector_ms = 0;

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
                                static_cast<unsigned long>(kRelayRate));
                    handled = true;
                } else if (std::strncmp(g_console_line, "vector", 6) == 0) {
                    if (g_latest_window_vector_valid) {
                        std::printf("{\"type\":\"window_vector\",\"valid\":true,"
                                    "\"t_ms\":%lu,\"vector\":[",
                                    static_cast<unsigned long>(g_latest_window_vector_ms));
                        for (std::size_t i = 0; i < context::kFeatureCount; ++i) {
                            std::printf("%s%.6f", i == 0 ? "" : ",",
                                        static_cast<double>(g_latest_window_vector[i]));
                        }
                        std::printf("]}\n");
                    } else {
                        std::printf("{\"type\":\"window_vector\",\"valid\":false}\n");
                    }
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
                } else if (std::strncmp(g_console_line, "voiceprint ", 11) == 0) {
                    const char* value = g_console_line + 11;
                    float score = 0.0f;
                    bool pending = false;
                    if (std::strncmp(value, "self", 4) == 0) {
                        g_voiceprint_self = true;
                    } else if (std::strncmp(value, "other", 5) == 0) {
                        g_voiceprint_self = false;
                    } else if (std::strncmp(value, "pending", 7) == 0) {
                        // Host 已启用声纹，但当前 embedding 没有可靠身份：撤销旧身份。
                        // pending 不是说话人，也不能重置 EQ、对话状态或控制帧序号。
                        pending = true;
                        g_voiceprint_self = false;
                    } else if (std::strncmp(value, "off", 3) == 0 ||
                               std::strncmp(value, "none", 4) == 0) {
                        // Host 明确停用声纹（或重启后不再带 --voiceprint）：立刻退出 veto，
                        // 回到 D16 的纯预判逻辑，不需要重启设备。
                        g_voiceprint_valid = false;
                        g_voiceprint_self = false;
                        g_voiceprint_score = 0.0f;
                        g_voiceprint_us = 0;
                        std::printf("voiceprint ack off\n");
                        handled = true;
                        goto voiceprint_done;
                    } else {
                        goto voiceprint_done;
                    }
                    std::sscanf(value + (pending ? 7 : (g_voiceprint_self ? 4 : 5)), "%f", &score);
                    g_voiceprint_valid = true;
                    g_voiceprint_score = score;
                    g_voiceprint_us = esp_timer_get_time();
                    // pending 只撤销说话人身份，不得重置持续控制器。worker 在安静环境下会
                    // 周期性回传 pending；整机 reset 会把控制帧序号、EQ 平滑状态和对话状态
                    // 一起清零，表现成 8→1/9→1 的假重启以及音量/EQ 抖动。
                    // 非 JSON 行：host 的协议解析器会忽略它，monitor 能看到。
                    std::printf("voiceprint ack %s %.2f\n",
                                pending ? "pending" : (g_voiceprint_self ? "self" : "other"),
                                static_cast<double>(score));
                    handled = true;
                voiceprint_done:;
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
    /// 只被控制任务访问：主机测试与固件跑的是同一份判据，不需要加锁。
    static bone::BoneVoice bone_voice;
    // D47：双麦取数。I2S 开 STEREO 后 stage 里是 L/R 交织样本；主判据、降采样、中继、
    // 录音**全部继续用 L 路**（行为与单麦完全一致），R 路只进 dual_mic 累加器做遥测。
    i2s_std_config_t audio_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(kSampleRate),
        .slot_cfg = I2S_STD_PHILIP_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                       I2S_SLOT_MODE_STEREO),
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
    format.channel = 2;  // D47：双路（ES7210 MIC1+MIC2 在 L/R 槽）
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
    // D47：每块现在是 L/R 交织（kReadBlockSamples 帧 × 2），容量按交织样本数计、翻倍。
    constexpr std::size_t kStageCapacity = 8 * kReadBlockSamples * 2;
    static_assert(kStageCapacity % (kReadBlockSamples * 2) == 0,
                  "staging capacity must be a whole number of interleaved blocks");
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
    // The 2 s model window is ~2.6 KB, and the model's percentile accumulator another ~2 KB
    // (two 256-float arrays: the sample ring and the sort copy). Both go on the heap for the
    // same reason as the buffers above -- this task has a 12 KB stack, and a module-level array
    // would hide that cost (see the note on `emit_capture_chunk`'s buffer).
    //
    // This is not hypothetical: the first build of this round put `ContextTiming` on the stack
    // and the audio task overflowed its stack within seconds of boot ("***ERROR*** A stack
    // overflow in task audio_control has been detected"), rebooting the device in a loop. The
    // `stack_high_water` field in the latency report is what makes the remaining margin
    // visible rather than a matter of hope; it is reported in bytes (this port defines
    // `portSTACK_TYPE` as uint8_t, so FreeRTOS' "words" are bytes here).
    auto* context_window = static_cast<context::ContextWindow*>(
        heap_caps_malloc(sizeof(context::ContextWindow), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    auto* context_timing = static_cast<context::ContextTiming*>(
        heap_caps_malloc(sizeof(context::ContextTiming), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    // 声音事件判断器（1 s × 15 维 + 地板跟踪，约 2.4 KB）。同样走堆：音频任务栈只有 16 KB。
    static_assert(sound_event::kBandCount == kBandCount, "事件判断与控制器频段数不一致");
    auto* sound_event_detector = static_cast<sound_event::Detector*>(
        heap_caps_malloc(sizeof(sound_event::Detector), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    auto* event_timing = static_cast<context::ContextTiming*>(
        heap_caps_malloc(sizeof(context::ContextTiming), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (decimated == nullptr || pending == nullptr || encode_buffer == nullptr ||
        context_window == nullptr || context_timing == nullptr || sound_event_detector == nullptr ||
        event_timing == nullptr) {
        ESP_LOGE(kTag, "Capture buffer allocation failed");
        vTaskDelete(nullptr);
    }
    new (context_window) context::ContextWindow();
    new (context_timing) context::ContextTiming();
    new (sound_event_detector) sound_event::Detector();
    new (event_timing) context::ContextTiming();

    NoiseAnalyzer analyzer;
    AdaptController controller;
    controller.set_evidence_policy(kEvidencePolicy);
    latency::LatencyProfiler profiler;
    latency::ProfilerOverhead overhead;
    // D47：双麦取数累加器（6 个 double，栈上安全）与降采样用的单路暂存块。
    // mono_block 用 static：任务独占、无重入；栈只有 16 KB，1 KB 的块不该再往里塞。
    dual_mic::Accumulator dual_mic_acc;
    static int16_t mono_block[kReadBlockSamples];
    int64_t next_latency_report_us = esp_timer_get_time() + kLatencyReportPeriodUs;
    uint32_t analysed_frames = 0;
    capture::Decimator decimator;
    uint32_t capture_sequence = 0;
    std::size_t pending_samples = 0;
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
        // During relay, capture must keep reading even when the slower AI analysis window is
        // full. The capture path below drains `stage` every pass and only copies into `filled`
        // when there is room. Coupling the hardware read to `filled` limited recording to four
        // blocks per analysis period: a 10-second command contained under two seconds of audio.
        const bool capture_needs_audio = g_record_seconds >= 0;
        // D47：`staged` / `kStageCapacity` 的单位是**交织样本数**（L+R 各一 = 2 个 int16），
        // `want` 仍是 L/R 帧数，一次最多读一块（512 帧 = 1024 交织样本）。
        if (staged < kStageCapacity &&
            (capture_needs_audio || filled + kReadBlockSamples <= kFrameSamplesMax)) {
            const std::size_t want = std::min(kReadBlockSamples, (kStageCapacity - staged) / 2);
            const int64_t read_started_us = esp_timer_get_time();
            read_result = esp_codec_dev_read(microphone, stage + staged,
                                             static_cast<int>(want * 2 * sizeof(int16_t)));
            const uint32_t read_us =
                static_cast<uint32_t>(esp_timer_get_time() - read_started_us);
            if (read_result != ESP_OK) {
                // Empty read: count its own bucket. Keeping one total and one success total
                // invited a uint32 subtraction that underflowed to 4294966 ms.
                if (g_record_seconds >= 0) capture_no_data_us += read_us;
            } else {
                staged += want * 2;
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
            if (staged < kReadBlockSamples * 2) break;
            if (filled + kReadBlockSamples > kFrameSamplesMax) break;
            // D47：窗口只存 L 路（偶数下标），R 路只进双麦累加器。
            for (std::size_t i = 0; i < kReadBlockSamples; ++i) {
                samples[filled + i] = stage[2 * i];
            }
            dual_mic_acc.push(stage, kReadBlockSamples);
            filled += kReadBlockSamples;
            got = kReadBlockSamples;
            const std::size_t remaining = staged - kReadBlockSamples * 2;
            for (std::size_t i = 0; i < remaining; ++i) {
                stage[i] = stage[kReadBlockSamples * 2 + i];
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
            while (staged >= kReadBlockSamples * 2) {
                // Relay and environment control consume the same microphone stream. Capture
                // used to starve analysis, so enabling relay silently froze VAD/EQ decisions.
                if (filled + kReadBlockSamples <= kFrameSamplesMax) {
                    // D47：窗口只存 L 路；R 路只进双麦累加器。
                    for (std::size_t i = 0; i < kReadBlockSamples; ++i) {
                        samples[filled + i] = stage[2 * i];
                    }
                    dual_mic_acc.push(stage, kReadBlockSamples);
                    filled += kReadBlockSamples;
                    if (filled > capture_max_filled) {
                        capture_max_filled = static_cast<uint32_t>(filled);
                    }
                }
                // D47：降采样/中继/录音仍走 L 路——先抽出单路块再喂 decimator。
                for (std::size_t i = 0; i < kReadBlockSamples; ++i) {
                    mono_block[i] = stage[2 * i];
                }
                const std::size_t produced =
                    decimator.process(mono_block, kReadBlockSamples, decimated,
                                      kDecimatedCapacity);
                ++capture_blocks_to_decimator;
                capture_samples_to_decimator += static_cast<uint32_t>(kReadBlockSamples);
                capture_decimated_samples += static_cast<uint32_t>(produced);
                capture_last_produced = static_cast<uint32_t>(produced);
                const std::size_t remaining = staged - kReadBlockSamples * 2;
                for (std::size_t i = 0; i < remaining; ++i) {
                    stage[i] = stage[kReadBlockSamples * 2 + i];
                }
                staged = remaining;
                for (std::size_t i = 0; i < produced; ++i) {
                    pending[pending_samples++] = decimated[i];
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

        // 只有**有限时长录音**才放宽控制节拍。无限流（`rec start`，也就是中继/环境声）
        // 必须保持 100 ms：中继是对话预判的执行端，把感知节拍降到 250 ms 等于自己把
        // 预判做慢。这个区别是环境声改走 S3 中继后才重要的。
        const bool finite_recording = g_record_seconds > 0;
        const float period_ms = finite_recording ? kCapturePeriodMs : kControlPeriodMs;
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
        // D47：帧级双麦遥测（只上报，不改任何决策）。与窗口同步：窗口只收 L 路，
        // 累加器每块喂交织数据，帧完成后汇总并清零。
        const dual_mic::Stats dual_stats = dual_mic::finish(dual_mic_acc);
        dual_mic_acc.reset();
        frame.mic2_valid = dual_stats.valid;
        frame.mic2_broadband_dbfs = dual_stats.right_dbfs;
        frame.mic_diff_db = dual_stats.diff_db;
        frame.mic_coherence = dual_stats.coherence;
        // 判据历史必须跟着输入一起断：无效帧（削波/非有限值）已经写进了时域闸门的历史，
        // 不丢掉的话，削波段和恢复后的干净段会被当成同一个连续声音。NoiseAnalyzer::reset()
        // 的文档要求调用，这里补上——在此之前主循环从来没调用过它。
        if (!frame.mic_valid) analyzer.reset();
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

        // ---- 自身语音证据（骨传导）----
        // 把这一拍之前攒下的 IMU 采样全部灌进 bone_voice，再用这一拍的语音带电平配对。
        // 配对是 1:1 的：控制拍 100 ms、IMU 100 Hz，一拍正好 10 个采样。
        ImuSample imu_sample;
        while (g_imu_queue != nullptr && xQueueReceive(g_imu_queue, &imu_sample, 0) == pdTRUE) {
            const float magnitude = std::sqrt(imu_sample.accel[0] * imu_sample.accel[0] +
                                              imu_sample.accel[1] * imu_sample.accel[1] +
                                              imu_sample.accel[2] * imu_sample.accel[2]);
            bone_voice.push_imu(magnitude, static_cast<float>(imu_sample.t_us) / 1000.0f);
        }
        // 语音带（第 8–16 段，约 997–3415 Hz）的平均电平，作为骨传导判据的音频侧包络。
        // 注意它**不是**判据用的 300–4000 Hz 语音带（main/controller.cpp 的 in_speech_band，
        // 第 3–16 段）；两者差在低频段，这里只用于与 IMU 包络做相关，不参与人声判定。
        float speech_band_dbfs = 0.0f;
        for (std::size_t band = 8; band <= 16; ++band) {
            speech_band_dbfs += frame.band_dbfs[band] / 9.0f;
        }
        const bone::Output bone_out =
            bone_voice.push_audio(speech_band_dbfs, static_cast<float>(now_us) / 1000.0f);
        OwnVoiceEvidence own_voice;
        own_voice.gravity = bone_out.gravity;
        own_voice.confidence = bone_out.confidence;
        own_voice.correlation = bone_out.correlation;
        own_voice.vibration_rms = bone_out.vibration_rms;
        own_voice.motion = bone_out.motion;
        own_voice.bone = g_imu_ok;
        own_voice.imu_ok = g_imu_ok;
        controller.set_own_voice_evidence(own_voice);

        // ---- 声音事件判断（键盘 / 风 / 说话 / 撞击）----
        // 必须放在 controller.update() **之前**：键盘/风/撞击要作为"不是对话"的否决条件，
        // 不然房间噪声/键盘会把 voice_likelihood 顶过门槛，环境声一开就关不上。
        //
        // 撞击（削波）帧不进入 1 s 事件窗：Detector 内部会清窗并报 artifact。这样"撞桌子"
        // 不会因为瞬态形状像键盘而被判成键盘。窗长 1 s，比 9 类模型的 2 s 窗短——
        // 键盘/阵风是短事件，2 s 会把它们平均掉。
        const int64_t event_started_us = esp_timer_get_time();
        const sound_event::Output sound_event_out = sound_event_detector->push(
            frame.band_dbfs, frame.broadband_dbfs, frame.mic_valid,
            static_cast<float>(now_us) / 1.0e6f);
        event_timing->record(static_cast<float>(esp_timer_get_time() - event_started_us));
        const bool non_voice_event =
            sound_event_out.valid &&
            (sound_event_out.cls == sound_event::Class::keyboard ||
             sound_event_out.cls == sound_event::Class::wind ||
             sound_event_out.cls == sound_event::Class::artifact);

        // 声纹门控（D28 step3）：self → 不压媒体、不抬歌曲 EQ、不开环境声；
        // other 或没有判定 → 按 D16 的预判逻辑走。判定超过 3 s 没有新行即过期，
        // 过期后不再影响任何决策——worker 挂掉或上位机重启不会把功能锁死。
        const VoiceprintDecision voiceprint =
            evaluate_voiceprint(g_voiceprint_valid, g_voiceprint_self,
                                now_us - g_voiceprint_us, kVoiceprintHoldUs);
        const bool external_self_voice = voiceprint.self_voice;
        EarbudCommand command =
            controller.update(frame, elapsed_ms, non_voice_event, external_self_voice);
        command.event.valid = sound_event_out.valid;
        command.event.class_name = sound_event::class_name(sound_event_out.cls);
        const int64_t decide_done_us = esp_timer_get_time();

        // ---- 听音情境模型（融合第二轮：只上报，不改任何决策）----
        //
        // 窗口输入按**训练口径**构造，两处刻意与 ear-context 那套不同，理由与实测都在
        // context_window.hpp 的文件头：
        //   * 语音那一维用未过时域闸门的逐帧乘积（features.py 的 voice_likelihood 就是它），
        //     不是 frame.voice_likelihood——后者 2026-10-16 起乘了 modulation_term()；
        //   * 时间五维用 when=None 的默认基线，不是硬编码的"周一 00:00"。
        // 运动两维保持 0（v0.2 是 motion=None 训的）。
        const VoiceFeatures frame_features = extract_voice_features(frame);
        context::ContextFrame wide_frame;
        wide_frame.band_dbfs = frame.band_dbfs;
        wide_frame.broadband_dbfs = frame.broadband_dbfs;
        wide_frame.voice_product = voice_likelihood_from_features(frame_features);
        wide_frame.spectral_flatness = frame_features.spectral_flatness;
        wide_frame.vocal_peakiness = frame_features.vocal_peakiness;
        wide_frame.vocal_ratio = frame_features.vocal_ratio;
        wide_frame.excited_vocal_bands =
            static_cast<float>(frame_features.excited_vocal_bands);
        wide_frame.t_s = static_cast<double>(now_us) / 1.0e6;
        if (frame.mic_valid) {
            context_window->push(wide_frame);
        } else {
            // An interrupted input is not a continuous sound: keeping the window would let the
            // frames before the gap form a slope and a modulation statistic together with the
            // frames after it. The analyser has the same rule for its own history.
            context_window->reset();
        }

        ContextResult context_result;
        if (context_window->full()) {
            float window_vector[context::kFeatureCount];
            context_window->build_vector(window_vector);
            for (std::size_t i = 0; i < context::kFeatureCount; ++i) {
                g_latest_window_vector[i] = window_vector[i];
            }
            g_latest_window_vector_ms = frame.timestamp_ms;
            g_latest_window_vector_valid = true;
            float logits[listening_model::kClassCount];
            const int64_t inference_started_us = esp_timer_get_time();
            listening_model::classify(window_vector, logits);
            const int64_t inference_us = esp_timer_get_time() - inference_started_us;
            // D46：上报 softmax 分布的 top1 + top2，而不是只有 argmax。混合环境
            // （人声+音乐、街道+持续噪声）里单标签会藏掉第二名；top2 与 mixed
            // 让"这一窗是混合的"可显示、可度量、可入账。决策仍不动（Round 2 纪律）。
            float probs[listening_model::kClassCount];
            context::softmax_probs(logits, listening_model::kClassCount, probs);
            std::size_t top1 = 0;
            std::size_t top2 = 1;
            if (probs[top2] > probs[top1]) {
                const std::size_t swap = top1;
                top1 = top2;
                top2 = swap;
            }
            for (std::size_t c = 2; c < listening_model::kClassCount; ++c) {
                if (probs[c] > probs[top1]) {
                    top2 = top1;
                    top1 = c;
                } else if (probs[c] > probs[top2]) {
                    top2 = c;
                }
            }
            context_result.valid = true;
            context_result.class_index = static_cast<uint8_t>(top1);
            context_result.score = probs[top1];
            context_result.top2_index = static_cast<uint8_t>(top2);
            context_result.top2_score = probs[top2];
            context_result.mixed = probs[top2] >= context::kMixedTop2Threshold;
            context_result.inference_us = static_cast<int32_t>(inference_us);
            context_result.class_name = listening_model::kClassNames[top1];
            context_timing->record(static_cast<float>(inference_us));

            // 说话 vs 非说话二分类学生（只上报）。输入与 9 类模型同一个 92 维窗向量。
            float gate_logits[speech_gate::kClassCount];
            const int64_t gate_started_us = esp_timer_get_time();
            speech_gate::classify(window_vector, gate_logits);
            command.speech_gate_us =
                static_cast<int32_t>(esp_timer_get_time() - gate_started_us);
            const float gate_max = gate_logits[0] > gate_logits[1]
                                       ? gate_logits[0] : gate_logits[1];
            const float gate_exp0 = std::exp(gate_logits[0] - gate_max);
            const float gate_exp1 = std::exp(gate_logits[1] - gate_max);
            command.speech_gate_score = gate_exp1 / (gate_exp0 + gate_exp1);
            command.speech_gate_valid = true;
        }
        // Assigned after `controller.update()`, and the controller never reads it back: that
        // separation is what makes this a no-behaviour-change round.
        command.context = context_result;

        // 风场景 EQ（D21）：只在放歌且没有对话时压低频抬升上限；对话优先。
        if (sound_event_out.valid && sound_event_out.cls == sound_event::Class::wind) {
            apply_wind_eq(command);
        }

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
            emit_latency_report(profiler, overhead, *context_timing, *event_timing,
                                analysed_frames);
            profiler.reset();
            context_timing->reset();
            event_timing->reset();
            next_latency_report_us = esp_timer_get_time() + kLatencyReportPeriodUs;
        }

        last_control_us = now_us;
        filled = 0;
    }
}
}  // namespace

/// IMU 采样任务：按 100 Hz 读数、进队列。
///
/// **它不初始化、也不做判定。** 初始化放在 app_main 里同步做完（见那里的注释：避免与
/// 音频任务抢同一条 I2C 总线），判定在控制任务里与音频包络配对 —— 这样 bone_voice 只需要
/// 单线程访问，主机测试与固件跑的是同一份逻辑。
static void imu_task(void*) {
    TickType_t last = xTaskGetTickCount();
    while (true) {
        ImuSample sample;
        if (imu_read(sample)) {
            if (xQueueSend(g_imu_queue, &sample, 0) != pdTRUE) ++g_imu_drops;
            else ++g_imu_samples;
        }
        // 当前 sdkconfig 的 FreeRTOS tick 是 10 ms；5 ms 会被 pdMS_TO_TICKS 截成 0，
        // xTaskDelayUntil 会直接断言并让设备重启。轮询间隔至少保持 1 tick。
        const TickType_t delay_ticks =
            std::max<TickType_t>(1, pdMS_TO_TICKS(kImuPollMs));
        vTaskDelayUntil(&last, delay_ticks);
    }
}

extern "C" void app_main(void) {
    ESP_LOGI(kTag, "Starting earbud-case-shaped edge audio controller");
    ESP_LOGI(kTag, "No autoplay. Built-in speaker remains silent.");
    ESP_LOGI(kTag, "Send 'rec start [seconds]' or 'rec stop' to capture the microphone.");
    bsp_display_start();
    bsp_display_lock(0);
    demo_ui_create();
    bsp_display_unlock();
    bsp_display_backlight_on();
    // 16 KB, up from 12 KB. Two measured reasons, both from the round that put the model on
    // this task:
    //   * `latency::LatencyProfiler profiler` lives on this stack and is 7 KB on its own
    //     (7 stages × 256 float samples), and `LatencyProfiler::stats()` sorts into another
    //     1 KB local array. The 10-second latency report is therefore the deepest path here.
    //   * With the model wired in at 12 KB the task overflowed its stack within seconds of
    //     boot ("***ERROR*** A stack overflow in task audio_control has been detected") and
    //     rebooted in a loop. The model's own working set is only ~500 bytes on the stack
    //     (92-float window vector, one context frame); the 2 KB percentile accumulator was
    //     the real cost and now lives on the heap with the window.
    // The `stack_high_water` field in the latency report is the live number; measure it again
    // after any addition to this task rather than trusting this comment.
    // ---- IMU 同步初始化，先于音频任务 ----
    //
    // 为什么不在采样任务里初始化：BMI270 和音频编解码器**共用 BSP 的 I2C 总线**
    // （bsp_i2c_init 是幂等的，共享本身没问题），但音频控制任务的优先级（6）高于 IMU 任务（4），
    // 由 IMU 任务去初始化就会被音频任务抢占，两次 I2C 驱动安装/设备注册可能交错。
    // ear-context 那个实验里两个任务是同级（5），不会互相抢占，所以照搬它的写法在这里并不等价。
    // 在 app_main 里同步做完，既拿到了"IMU 先于音频"的已验证顺序，又完全没有并发。
    g_imu_ok = imu_init();
    if (!g_imu_ok) {
        ESP_LOGW(kTag, "BMI270 未就绪：自身语音证据按 fail-closed 处理（不下压）");
    }

    g_imu_queue = xQueueCreate(kImuQueueDepth, sizeof(ImuSample));
    if (g_imu_queue != nullptr && g_imu_ok) {
        // 优先级低于音频控制任务：IMU 是证据源，不能反过来抢音频的节拍。
        // 栈取 6144：与 ear-context 里同一份 imu_read + 队列写法相同，那个尺寸是真机跑过的；
        // 4 KB 没验过，而栈溢出在这里只会表现为"偶尔丢采样"，很难查。
        xTaskCreatePinnedToCore(imu_task, "imu", 6144, nullptr, 4, nullptr, 1);
    } else if (g_imu_queue == nullptr) {
        ESP_LOGW(kTag, "IMU 队列创建失败：自身语音证据按 fail-closed 处理（不下压）");
    }

    xTaskCreatePinnedToCore(processing_task, "audio_control", 16384, nullptr, 6, nullptr, 1);
}
