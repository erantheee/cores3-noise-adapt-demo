// Tests for main/context_window.hpp: the 92-dim model input.
//
// Why this suite exists at all: the window builder is a hand port of
// `experiments/listening-model/scripts/features.py::window_vector()`, and a wrong index in it
// does not raise anything -- the model simply returns a different class, quietly. The golden
// vector in `context_window_golden.h` is produced by the training-side implementation on a
// fixed recipe, so the two implementations are compared dimension by dimension here.
//
// Regenerate the golden after any change to the recipe or to features.py:
//     .venv/bin/python experiments/listening-model/scripts/emit_window_golden.py
#include <cmath>
#include <cstring>
#include <iostream>
#include <string>

#include "../main/context_window.hpp"
#include "context_window_golden.h"

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const std::string& name) {
    if (condition) {
        ++passed;
        std::cout << "PASS " << name << '\n';
    } else {
        ++failed;
        std::cout << "FAIL " << name << '\n';
    }
}

/// The recipe, copied from emit_window_golden.py. Every constant is exactly representable in
/// binary, so the C++ float32 frame and the Python float64 frame hold the same numbers.
context::ContextFrame make_frame(int i) {
    context::ContextFrame f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        f.band_dbfs[band] = static_cast<float>(-60.0 - 2.0 * static_cast<double>(band) -
                                               0.5 * static_cast<double>(i));
    }
    f.broadband_dbfs = static_cast<float>(-50.0 + 0.5 * static_cast<double>(i));
    f.voice_product = (i % 3 == 0) ? 0.75f : 0.25f;
    f.spectral_flatness = static_cast<float>(0.5 - 0.015625 * static_cast<double>(i));
    f.vocal_peakiness = static_cast<float>(0.25 + 0.03125 * static_cast<double>(i));
    f.vocal_ratio = static_cast<float>(0.375 + 0.03125 * static_cast<double>(i));
    f.excited_vocal_bands = static_cast<float>(i % 7);
    f.t_s = 100.0 + 0.125 * static_cast<double>(i);
    return f;
}

context::ContextWindow make_window(int first, int count) {
    context::ContextWindow window;
    for (int k = 0; k < count; ++k) window.push(make_frame(first + k));
    return window;
}

/// float32 output against a float64 reference: the tolerance is set by float32 rounding
/// (~1e-5 at these magnitudes), not by any slack in the comparison.
bool close_enough(float actual, float expected) {
    const double tolerance = 1.0e-4 * std::fmax(1.0, std::fabs(static_cast<double>(expected)));
    return std::fabs(static_cast<double>(actual) - static_cast<double>(expected)) <= tolerance;
}
}  // namespace

int main() {
    // ---- 1) The window only has a defined vector when it is full ----
    {
        context::ContextWindow window = make_window(0, context::kWindowFrames - 1);
        float out[context::kFeatureCount];
        std::memset(out, 0, sizeof(out));
        check(!window.full(), "19 帧时窗口未满");
        check(!window.build_vector(out), "未满窗时 build_vector 返回 false");
        check(out[0] == 0.0f, "未满窗时不写输出（不留半窗统计量）");

        window.push(make_frame(context::kWindowFrames - 1));
        check(window.full(), "20 帧时窗口满");
        check(window.build_vector(out), "满窗时 build_vector 返回 true");
    }

    // ---- 2) Dimension-by-dimension agreement with the training-side implementation ----
    {
        context::ContextWindow window = make_window(0, context::kWindowFrames);
        float out[context::kFeatureCount];
        const bool built = window.build_vector(out);
        check(built, "满窗可构建向量");
        int mismatches = 0;
        int worst_index = -1;
        double worst = 0.0;
        for (int d = 0; d < context::kFeatureCount; ++d) {
            const double expected = context_golden::kExpected[d];
            const double diff = std::fabs(static_cast<double>(out[d]) - expected);
            if (!close_enough(out[d], static_cast<float>(expected))) ++mismatches;
            if (diff > worst) {
                worst = diff;
                worst_index = d;
            }
        }
        check(mismatches == 0, "92 维全部与 features.py 的金标准一致（不一致维数 == 0）");
        if (mismatches != 0) {
            std::cout << "    最大偏差维 " << worst_index << "：" << worst << '\n';
        }
        check(worst < 1.0e-3, "92 维最大绝对偏差 < 1e-3");
    }

    // ---- 3) Ring order: pushing more than 20 frames must keep the newest 20 ----
    {
        context::ContextWindow rolled = make_window(0, context::kWindowFrames + 5);
        context::ContextWindow fresh = make_window(5, context::kWindowFrames);
        float a[context::kFeatureCount];
        float b[context::kFeatureCount];
        rolled.build_vector(a);
        fresh.build_vector(b);
        bool same = true;
        for (int d = 0; d < context::kFeatureCount; ++d) {
            if (a[d] != b[d]) same = false;
        }
        check(same, "环形缓冲绕回后与「最近 20 帧」逐位一致");
    }

    // ---- 4) The three calibration points that were measured, not assumed ----
    {
        context::ContextWindow window = make_window(0, context::kWindowFrames);
        float out[context::kFeatureCount];
        window.build_vector(out);
        check(out[85] == 0.0f && out[86] == 0.0f,
              "运动两维为 0（v0.2 是 motion=None 训的）");
        check(out[88] == -1.0f,
              "hour_cos 用 when=None 基线（-1），不是 ear-context 硬编码的 +1");
        check(std::fabs(out[89] - 0.974928f) < 1.0e-5f,
              "dow_sin 用 when=None 基线（0.974928），不是 0");
        check(std::fabs(out[90] + 0.222521f) < 1.0e-5f,
              "dow_cos 用 when=None 基线（-0.222521），不是 +1");
        check(out[91] == 0.0f, "is_weekend = 0（周三）");
        // vl_mean must come from the ungated product (0.75/0.25 pattern -> 0.425), not
        // from NoiseAnalyzer's gated voice_likelihood.
        check(std::fabs(out[76] - 0.425f) < 1.0e-6f,
              "vl_mean 用未过时域闸门的逐帧乘积（0.425），不是闸门后的值");
        check(std::fabs(out[78] - 0.35f) < 1.0e-6f, "vl_active_frac = 7/20");
        check(out[79] == 13.0f, "vl_transitions = 13（0.75/0.25 交替，越 0.55 的边沿计数）");
        // 电平每帧固定 +0.5 dB，没有任何一步超过 3 dB，所以步进计数必须是 0。
        // 这条同时钉住了阈值方向：写成 "< 3" 就会得到 19。
        check(out[84] == 0.0f, "level_steps = 0（相邻帧电平差固定 0.5 dB，不越 3 dB 阈值）");
    }

    // ---- 5) reset() must clear, not just rewind ----
    {
        context::ContextWindow window = make_window(0, context::kWindowFrames);
        window.reset();
        check(!window.full() && window.size() == 0, "reset() 后窗口为空");
        float out[context::kFeatureCount];
        check(!window.build_vector(out), "reset() 后 build_vector 返回 false");
    }

    // ---- 6) The whole chain: window -> exported header -> a class ----
    //
    // This pins the end-to-end numeric path (builder + listening_model.h). It is a *pipe*
    // check, not an accuracy claim: the recipe is synthetic and has no true label.
    {
        context::ContextWindow window = make_window(0, context::kWindowFrames);
        float features[context::kFeatureCount];
        window.build_vector(features);
        float logits[listening_model::kClassCount];
        listening_model::classify(features, logits);
        std::size_t index = 0;
        float score = 0.0f;
        context::top_class(logits, listening_model::kClassCount, &index, &score);
        std::cout << "    合成配方 -> " << listening_model::kClassNames[index] << " ("
                  << score << ")\n";
        check(index < listening_model::kClassCount, "分类索引在范围内");
        check(score > 0.0f && score <= 1.0f, "softmax 置信度在 (0, 1]");
        check(std::string(listening_model::kVersion) == "v0.2-s3",
              "模型版本是 v0.2-s3（融合的是哪一份权重，写在这里）");
    }

    std::cout << "\n" << passed << " 条通过、" << failed << " 条失败\n";
    return failed == 0 ? 0 : 1;
}
