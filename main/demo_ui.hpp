#pragma once

#include "controller.hpp"

void demo_ui_create();
void demo_ui_update(const AnalysisFrame& frame, const EarbudCommand& command,
                    float processing_ms, uint32_t free_heap);
