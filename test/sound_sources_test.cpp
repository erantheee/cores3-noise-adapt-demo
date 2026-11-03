#include <cstdio>
#include <limits>
#include <string_view>
#include "sound_sources_model.h"

int main() {
    int failures = 0;
    const auto check = [&failures](bool value, const char* message) {
        if (!value) { std::printf("FAIL %s\n", message); ++failures; }
    };
    float x[92] = {};
    auto r = sound_sources::evaluate(nullptr, x);
    check(!r.valid && r.status == std::string_view("model_unavailable"), "no model is not silence");
    check(!sound_sources::evaluate(nullptr, nullptr).valid, "no partial window inference");
    float mean[92] = {}, scale[92], w1[24*92] = {}, b1[24] = {};
    float w2[7*24] = {}, b2[7] = {2, -2, 3, 4, -3, -4, 0}, thresholds[7];
    bool trained[7] = {true, true, true, true, true, true, false};
    bool validated[7] = {};
    for (auto& v : scale) v = 1;
    for (auto& v : thresholds) v = 0.5f;
    sound_sources::Model fixture{"synthetic-test", mean, scale, w1, b1, w2, b2,
                                  thresholds, trained, validated};
    r = sound_sources::evaluate(&fixture, x);
    check(r.valid && r.detected[0] && r.detected[2] && r.detected[3],
          "traffic, speech and wind coexist without argmax");
    check(r.scores[0] + r.scores[2] > 1.0f, "scores are independent, not softmax");
    check(!r.available[6] && !r.detected[6], "untrained channel is unavailable");
    for (auto value : r.validated) check(!value, "fixture is not field validated");
    x[1] = std::numeric_limits<float>::quiet_NaN();
    r = sound_sources::evaluate(&fixture, x);
    check(!r.valid && !r.detected[0], "invalid inputs clear all detections");
    x[1] = 0;
    scale[0] = 0;
    check(!sound_sources::evaluate(&fixture, x).valid, "invalid scale rejected");
    scale[0] = 1;
    b2[0] = 1000;
    b2[1] = -1000;
    r = sound_sources::evaluate(&fixture, x);
    check(r.valid && r.scores[0] == 1 && r.scores[1] == 0, "stable sigmoid extremes");
    sound_sources::Continuity continuity;
    for (unsigned t = 100; t < 2000; t += 100)
        check(!continuity.push(t, true), "partial windows remain invalid");
    check(continuity.push(2000, true), "twenty contiguous frames ready");
    check(!continuity.push(2500, true), "gap invalidates telemetry window");
    for (unsigned t = 2600; t <= 4300; t += 100) continuity.push(t, true);
    check(continuity.push(4400, true), "twenty fresh frames recover after gap");
    check(!continuity.push(4500, false), "clipping clears readiness");
    check(!continuity.push(4500, true), "duplicate timestamp cannot restore readiness");
    std::printf("sound sources: %s\n", failures ? "FAIL" : "PASS (synthetic behavior only)");
    return failures ? 1 : 0;
}
