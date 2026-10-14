#include "latency_profiler.hpp"

#include <algorithm>
#include <cmath>

namespace latency {
namespace {
constexpr std::size_t index_of(Stage stage) { return static_cast<std::size_t>(stage); }

bool usable_duration(float ms) { return std::isfinite(ms) && ms >= 0.0f; }
}  // namespace

const char* stage_name(Stage stage) {
    switch (stage) {
        case Stage::capture: return "capture";
        case Stage::analyse: return "analyse";
        case Stage::embed: return "embed";
        case Stage::decide: return "decide";
        case Stage::transport: return "transport";
        case Stage::apply: return "apply";
        case Stage::render: return "render";
    }
    return "unknown";
}

bool is_local_stage(Stage stage) {
    switch (stage) {
        case Stage::capture:
        case Stage::analyse:
        case Stage::embed:
        case Stage::decide:
            return true;
        case Stage::transport:
        case Stage::apply:
        case Stage::render:
            return false;
    }
    return false;
}

float nearest_rank_percentile(const float* sorted, std::size_t count, float fraction) {
    if (sorted == nullptr || count == 0) return 0.0f;
    const float clamped = std::clamp(fraction, 0.0f, 1.0f);
    // Nearest rank: ceil(fraction * n) with a one-based rank. For fraction 0 that is the
    // first element, for fraction 1 it is the last.
    std::size_t rank = static_cast<std::size_t>(std::ceil(clamped * static_cast<float>(count)));
    if (rank == 0) rank = 1;
    if (rank > count) rank = count;
    return sorted[rank - 1];
}

void LatencyProfiler::record(Stage stage, float ms) {
    if (!usable_duration(ms)) {
        ++rejected_;
        return;
    }
    const std::size_t slot = index_of(stage);
    samples_[slot][next_[slot]] = ms;
    next_[slot] = (next_[slot] + 1) % kWindowSize;
    if (count_[slot] < kWindowSize) ++count_[slot];
}

void LatencyProfiler::record_peer_latency(Stage stage, float ms) {
    // Same storage. Separated by name so call sites make it obvious that the value came
    // from across the link rather than from this device's own clock.
    record(stage, ms);
}

StageStats LatencyProfiler::stats(Stage stage) const {
    StageStats result;
    const std::size_t slot = index_of(stage);
    const std::size_t n = count_[slot];
    result.samples = n;
    if (n == 0) return result;

    std::array<float, kWindowSize> scratch{};
    std::copy_n(samples_[slot].begin(), n, scratch.begin());
    std::sort(scratch.begin(), scratch.begin() + static_cast<std::ptrdiff_t>(n));

    double sum = 0.0;
    for (std::size_t i = 0; i < n; ++i) sum += scratch[i];

    result.min_ms = scratch[0];
    result.max_ms = scratch[n - 1];
    result.mean_ms = static_cast<float>(sum / static_cast<double>(n));
    result.p50_ms = nearest_rank_percentile(scratch.data(), n, 0.50f);
    result.p95_ms = nearest_rank_percentile(scratch.data(), n, 0.95f);
    return result;
}

float LatencyProfiler::total_p50_ms() const {
    float total = 0.0f;
    for (std::size_t slot = 0; slot < kStageCount; ++slot) {
        total += stats(static_cast<Stage>(slot)).p50_ms;
    }
    return total;
}

float LatencyProfiler::total_p95_ms() const {
    float total = 0.0f;
    for (std::size_t slot = 0; slot < kStageCount; ++slot) {
        total += stats(static_cast<Stage>(slot)).p95_ms;
    }
    return total;
}

std::size_t LatencyProfiler::samples(Stage stage) const { return count_[index_of(stage)]; }

void LatencyProfiler::reset() {
    for (auto& buffer : samples_) buffer.fill(0.0f);
    count_.fill(0);
    next_.fill(0);
    rejected_ = 0;
}

void ProfilerOverhead::record(float overhead_ms) {
    if (!usable_duration(overhead_ms)) return;
    sum_ms_ += overhead_ms;
    max_ms_ = std::max(max_ms_, overhead_ms);
    ++count_;
}

float ProfilerOverhead::mean_ms() const {
    if (count_ == 0) return 0.0f;
    return static_cast<float>(sum_ms_ / static_cast<double>(count_));
}

float ProfilerOverhead::max_ms() const { return max_ms_; }

float ProfilerOverhead::budget_share() const {
    if (frame_budget_ms_ <= 0.0f) return 0.0f;
    return mean_ms() / frame_budget_ms_;
}

bool ProfilerOverhead::within_budget(float limit_share) const {
    return budget_share() <= limit_share;
}

}  // namespace latency
