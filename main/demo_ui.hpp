#pragma once

#include "controller.hpp"

struct MacAudioRoute {
    char device_name[48] = "AirPods Pro";
    bool mac_connected = false;
    bool audio_output_active = false;
};

void demo_ui_create();
void demo_ui_update(const AnalysisFrame& frame, const EarbudCommand& command,
                    const MacAudioRoute& route, float processing_ms, uint32_t free_heap);
