#include <cmath>
#include <cstdio>

#include "speech_gate_golden.h"
#include "speech_gate_model.h"

namespace {
bool close(float a, float b) { return std::fabs(a - b) <= 3.0e-4f; }
}  // namespace

int main() {
    int passed = 0;
    int failed = 0;
    for (std::size_t sample = 0; sample < speech_gate_golden::kSampleCount; ++sample) {
        float logits[speech_gate::kClassCount];
        speech_gate::classify(speech_gate_golden::kInputs[sample], logits);
        const float top = logits[0] > logits[1] ? logits[0] : logits[1];
        const float e0 = std::exp(logits[0] - top);
        const float e1 = std::exp(logits[1] - top);
        const float prob = e1 / (e0 + e1);
        const float expected = speech_gate_golden::kExpectedProb[sample];
        if (close(prob, expected)) {
            ++passed;
        } else {
            ++failed;
            std::printf("sample %zu: got %.6f want %.6f\n", sample, static_cast<double>(prob),
                        static_cast<double>(expected));
        }
        if (!(prob >= 0.0f && prob <= 1.0f)) {
            ++failed;
            std::printf("sample %zu: probability %.6f out of [0,1]\n", sample,
                        static_cast<double>(prob));
        }
    }
    std::printf("speech_gate: %d/%d passed\n", passed, passed + failed);
    return failed == 0 ? 0 : 1;
}
