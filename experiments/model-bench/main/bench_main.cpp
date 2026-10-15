/// 端侧听音情境模型基准（CoreS3 / ESP32-S3 @240 MHz）。
///
/// 测两件事，都是"设备说了算"的数字：
///   1. 正确性：设备端 classify() 的预测必须与导出时的期望标签逐条一致——不一致说明
///      导出物或设备端编译有问题，这时报任何耗时数字都没有意义。
///   2. 耗时：4000 次前向的 p50 / p95 / max（微秒）。这是这套流程里第一笔"真正的神经网络
///      负载"的端侧数字；此前 embed 段报 0 是因为没有实现，不是因为免费。
///
/// 输出为 JSON 行（与 cores3-noise-adapt-demo 的日志口径一致，便于上位机解析）。

#include <cstdint>
#include <cstdio>
#include <cstring>

#include "esp_heap_caps.h"
#include "esp_timer.h"

#include "listening_model.h"
#include "test_vectors.h"

namespace {
constexpr int kTimingRuns = 4000;

int compare_i64(const void* a, const void* b) {
    const int64_t x = *static_cast<const int64_t*>(a);
    const int64_t y = *static_cast<const int64_t*>(b);
    return (x > y) - (x < y);
}
}  // namespace

extern "C" void app_main() {
    const unsigned free_before =
        static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_8BIT));
    std::printf(
        "{\"type\":\"model_bench\",\"stage\":\"start\",\"model\":\"%s\","
        "\"features\":%u,\"classes\":%u,\"vectors\":%d,\"free_heap_before\":%u}\n",
        listening_model::kVersion,
        static_cast<unsigned>(listening_model::kFeatureCount),
        static_cast<unsigned>(listening_model::kClassCount),
        test_vectors::kCount, free_before);

    // ---- 1) 正确性 ----
    int correct = 0;
    int first_mismatch = -1;
    for (int i = 0; i < test_vectors::kCount; ++i) {
        float logits[listening_model::kClassCount];
        listening_model::classify(test_vectors::kData[i], logits);
        int best = 0;
        for (std::size_t c = 1; c < listening_model::kClassCount; ++c) {
            if (logits[c] > logits[best]) best = static_cast<int>(c);
        }
        if (best == test_vectors::kExpected[i]) {
            ++correct;
        } else if (first_mismatch < 0) {
            first_mismatch = i;
        }
    }
    std::printf(
        "{\"type\":\"model_bench\",\"stage\":\"correctness\",\"vectors\":%d,"
        "\"correct\":%d,\"first_mismatch\":%d}\n",
        test_vectors::kCount, correct, first_mismatch);

    // ---- 2) 耗时 ----
    static int64_t runs[kTimingRuns];
    float scratch[listening_model::kFeatureCount];
    volatile float sink = 0.0f;
    for (int i = 0; i < kTimingRuns; ++i) {
        std::memcpy(scratch, test_vectors::kData[i % test_vectors::kCount],
                    sizeof(scratch));
        // 改一位输入，防止编译器把整条推理常量化（真机上这种优化会直接把数字变成 0）
        scratch[i % static_cast<int>(listening_model::kFeatureCount)] += 1.0e-4f;
        float logits[listening_model::kClassCount];
        const int64_t t0 = esp_timer_get_time();
        listening_model::classify(scratch, logits);
        runs[i] = esp_timer_get_time() - t0;
        sink += logits[0];
    }
    std::qsort(runs, kTimingRuns, sizeof(runs[0]), compare_i64);
    const int64_t p50 = runs[kTimingRuns / 2];
    const int64_t p95 = runs[(kTimingRuns * 95) / 100];
    const int64_t max_us = runs[kTimingRuns - 1];
    const unsigned free_after =
        static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_8BIT));
    std::printf(
        "{\"type\":\"model_bench\",\"stage\":\"timing\",\"runs\":%d,"
        "\"p50_us\":%lld,\"p95_us\":%lld,\"max_us\":%lld,\"free_heap_after\":%u,"
        "\"sink\":%d}\n",
        kTimingRuns, static_cast<long long>(p50), static_cast<long long>(p95),
        static_cast<long long>(max_us), free_after,
        static_cast<int>(sink * 1000.0f));
    std::printf("{\"type\":\"model_bench\",\"stage\":\"done\"}\n");
}
