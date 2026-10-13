#include <cstdio>
#include <cstring>

#include "bsp/esp-bsp.h"
#include "controller.hpp"
#include "demo_ui.hpp"
#include "esp_codec_dev.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace {
constexpr uint32_t kSampleRate = 48000;
constexpr std::size_t kFrameSamples = 2048;
constexpr float kControlPeriodMs = 150.0f;
const char* kTag = "cores3_noise_adapt";

void emit_command_json(const EarbudCommand& command) {
    std::printf("{\"type\":\"earbud_command\",\"schema\":%lu,\"sequence\":%lu,"
                "\"timestamp_ms\":%lu,\"speech_gain_db\":%.2f,"
                "\"noise_reduction_db\":%.2f,\"media_percent\":%u,"
                "\"state\":\"%s\",\"environment\":\"%s\","
                "\"confidence\":%.2f,\"link\":\"simulated\"}\n",
                static_cast<unsigned long>(command.schema_version),
                static_cast<unsigned long>(command.sequence),
                static_cast<unsigned long>(command.timestamp_ms),
                command.speech_gain_db, command.noise_reduction_db, command.media_percent,
                adapt_state_name(command.state), environment_name(command.environment),
                command.confidence);
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
    esp_codec_dev_sample_info_t format = {
        .sample_rate = kSampleRate,
        .channel = 1,
        .bits_per_sample = 16,
    };
    ESP_ERROR_CHECK(esp_codec_dev_set_in_gain(microphone, 36.0f));
    ESP_ERROR_CHECK(esp_codec_dev_open(microphone, &format));

    auto* samples = static_cast<int16_t*>(
        heap_caps_malloc(kFrameSamples * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (samples == nullptr) {
        ESP_LOGE(kTag, "Audio buffer allocation failed");
        vTaskDelete(nullptr);
    }

    NoiseAnalyzer analyzer;
    AdaptController controller;
    int64_t last_control_us = esp_timer_get_time();
    while (true) {
        const esp_err_t read_result = esp_codec_dev_read(
            microphone, samples, kFrameSamples * sizeof(int16_t));
        const int64_t now_us = esp_timer_get_time();
        if ((now_us - last_control_us) < static_cast<int64_t>(kControlPeriodMs * 1000.0f)) continue;

        const int64_t started_us = esp_timer_get_time();
        AnalysisFrame frame = analyzer.analyze(samples, kFrameSamples,
            static_cast<uint32_t>(now_us / 1000));
        if (read_result != ESP_OK) frame.mic_valid = false;
        const float elapsed_ms = static_cast<float>(now_us - last_control_us) / 1000.0f;
        EarbudCommand command = controller.update(frame, elapsed_ms);
        const float processing_ms = static_cast<float>(esp_timer_get_time() - started_us) / 1000.0f;
        emit_command_json(command);

        bsp_display_lock(0);
        demo_ui_update(frame, command, processing_ms, esp_get_free_heap_size());
        bsp_display_unlock();
        last_control_us = now_us;
    }
}
}  // namespace

extern "C" void app_main(void) {
    ESP_LOGI(kTag, "Starting earbud-case-shaped edge audio controller");
    ESP_LOGI(kTag, "No autoplay. Built-in speaker remains silent.");
    bsp_display_start();
    bsp_display_lock(0);
    demo_ui_create();
    bsp_display_unlock();
    bsp_display_backlight_on();
    xTaskCreatePinnedToCore(processing_task, "audio_control", 12288, nullptr, 6, nullptr, 1);
}
