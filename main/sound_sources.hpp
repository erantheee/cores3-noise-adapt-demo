#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>

// Independent sigmoid outputs. No controller or dialogue code consumes this type.
namespace sound_sources {
constexpr std::size_t kFeatures = 92;
constexpr std::size_t kHidden = 24;
constexpr std::size_t kClasses = 7;
constexpr const char* kNames[kClasses] = {
    "traffic", "horn_alarm", "speech", "wind", "music", "keyboard", "mechanical"};

struct Model {
    const char* version;
    const float* mean;
    const float* scale;
    const float* w1;
    const float* b1;
    const float* w2;
    const float* b2;
    const float* thresholds;
    const bool* trained;
    // Offline metrics never establish S3 field validation.
    const bool* field_validated;
};

struct Result {
    const char* status = "window_incomplete";
    const char* version = "untrained";
    bool valid = false;
    std::array<float, kClasses> scores{};
    std::array<bool, kClasses> available{};
    std::array<bool, kClasses> detected{};
    std::array<bool, kClasses> validated{};
    int32_t inference_us = 0;
};

// Separate continuity gate so telemetry cannot reset the legacy control window.
class Continuity {
public:
    bool push(uint32_t timestamp_ms, bool input_valid) {
        if (!input_valid) { count_ = 0; last_ = timestamp_ms; return false; }
        const uint32_t delta = timestamp_ms - last_;
        if (count_ && (delta < 50 || delta > 150)) count_ = 0;
        last_ = timestamp_ms;
        count_ = std::min<std::size_t>(20, count_ + 1);
        return count_ == 20;
    }
private:
    uint32_t last_ = 0;
    std::size_t count_ = 0;
};

inline Result evaluate(const Model* model, const float* x) {
    Result out;
    if (!x) return out;
    for (std::size_t i = 0; i < kFeatures; ++i) {
        if (!std::isfinite(x[i])) { out.status = "input_invalid"; return out; }
    }
    if (!model) { out.status = "model_unavailable"; return out; }
    out.version = model->version;
    float z[kFeatures];
    float hidden[kHidden];
    for (std::size_t i = 0; i < kFeatures; ++i) {
        if (!std::isfinite(model->scale[i]) || model->scale[i] <= 0) {
            out.status = "model_invalid"; return out;
        }
        z[i] = (x[i] - model->mean[i]) / model->scale[i];
    }
    for (std::size_t h = 0; h < kHidden; ++h) {
        float a = model->b1[h];
        for (std::size_t i = 0; i < kFeatures; ++i) a += model->w1[h*kFeatures+i] * z[i];
        if (!std::isfinite(a)) { out.status = "model_invalid"; return out; }
        hidden[h] = std::max(0.0f, a);
    }
    for (std::size_t c = 0; c < kClasses; ++c) {
        if (!model->trained[c]) continue;
        float a = model->b2[c];
        for (std::size_t h = 0; h < kHidden; ++h) a += model->w2[c*kHidden+h] * hidden[h];
        if (!std::isfinite(a) || !std::isfinite(model->thresholds[c]) ||
            model->thresholds[c] <= 0 || model->thresholds[c] >= 1) {
            Result invalid;
            invalid.status = "model_invalid";
            return invalid;
        }
        const float e = std::exp(-std::fabs(a));
        out.scores[c] = a >= 0 ? 1.0f / (1.0f + e) : e / (1.0f + e);
        out.available[c] = true;
        out.detected[c] = out.scores[c] >= model->thresholds[c];
        out.validated[c] = model->field_validated[c];
    }
    out.valid = true;
    out.status = "ready";
    return out;
}
}  // namespace sound_sources
