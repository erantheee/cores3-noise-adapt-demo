#include <cmath>
#include <cstdio>
#include <cstring>

#include "scene_classifier.hpp"
#include "scene_golden.h"

namespace {
int failures = 0;

void check(bool ok, const char* what) {
    if (!ok) {
        std::printf("FAIL %s\n", what);
        ++failures;
    }
}

float shape_vector[92];
}  // namespace

int main() {
    for (const auto& item : scene_golden::kCases) {
        for (int i = 0; i < 92; ++i) shape_vector[i] = item.vector[i];
        for (int i = 0; i < 24; ++i) shape_vector[i] -= item.vector[72];
        float logits[3];
        scene_model::classify(shape_vector, logits);
        for (int c = 0; c < 3; ++c) {
            // 头部权重以 float32 保存，Python 导出用 float64 折叠归一化；0.02 的绝对差
            // 是这条 92 维点积的数值噪声上限。真正要钉死的是 argmax 与类别名。
            if (std::fabs(logits[c] - item.logits[c]) > 2e-2f) {
                std::printf("FAIL %s logit[%d] %.6f vs %.6f\n", item.label, c,
                            static_cast<double>(logits[c]),
                            static_cast<double>(item.logits[c]));
                ++failures;
            }
        }
        int argmax = 0;
        for (int c = 1; c < 3; ++c) {
            if (logits[c] > logits[argmax]) argmax = c;
        }
        check(argmax == item.argmax, "golden argmax");

        scene::Classifier classifier;
        const scene::Result result = classifier.update(item.vector);
        check(result.valid, "golden valid");
        check(std::strcmp(scene::name(result.kind), item.label) == 0,
              "classifier label matches golden source");
    }

    // Quiet gate: -80 dBFS is well below the room-floor threshold.
    {
        float v[92] = {};
        for (int i = 0; i < 24; ++i) v[i] = -80.0f;
        v[72] = -80.0f;
        scene::Classifier c;
        const auto r = c.update(v);
        check(r.kind == SceneKind::quiet, "quiet gate");
    }

    // Speech gate: high voice mean + flatness + vocal ratio should beat the market head.
    {
        float v[92] = {};
        for (int i = 0; i < 24; ++i) v[i] = -35.0f;
        v[72] = -35.0f;
        v[76] = 0.95f;
        v[80] = 0.20f;
        v[82] = 0.80f;
        scene::Classifier c;
        const auto r = c.update(v);
        check(r.kind == SceneKind::speech, "speech gate");
    }

    // Firmware four-class says VOICE: the scene must not claim market from a single talker.
    {
        float v[92] = {};
        for (int i = 0; i < 24; ++i) v[i] = -35.0f;
        v[72] = -35.0f;
        v[76] = 0.40f;
        scene::Classifier c;
        const auto r = c.update(v, true);
        check(r.kind == SceneKind::speech, "voice_likely overrides market head");
    }

    if (failures == 0) {
        std::printf("scene_test: all checks passed (%zu golden cases)\n",
                    sizeof(scene_golden::kCases) / sizeof(scene_golden::kCases[0]));
        return 0;
    }
    std::printf("scene_test: %d failure(s)\n", failures);
    return 1;
}
