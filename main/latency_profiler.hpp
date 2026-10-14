#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

/// Rolling latency statistics for the end-to-end decision path.
///
/// "Is the AI processing delay acceptable?" cannot be answered with a single number: the
/// total is dominated by stages the device does not control (today the Bluetooth audio
/// link at 311 ms) while the stages it does control are milliseconds. The only useful
/// answer is a per-stage breakdown with percentiles. This class collects that.
///
/// The pipeline stages, in the order energy actually flows:
///
///   0 capture      interval between consecutive analyses at the current cadence
///   1 analyse      24-band analysis for one frame
///   2 embed        speaker-embedding inference for one frame (0 when disabled)
///   3 decide       controller update
///   4 transport    command handed to the link until the acknowledgement arrives
///   5 apply        host-side time to act on the command
///   6 render       host-side device latency before sound leaves the speaker
///
/// Stages 4-6 are reported by the peer and fed in through `record_peer_latency`. Stages
/// 0-3 are measured on this device.
///
/// On what `capture` actually measures: it is the wall interval between two analyses, so
/// it is a true loop period, but it is not the hardware buffering delay on its own. That
/// would need I2S DMA introspection, which this round does not do. Read it as "how often
/// the decision is refreshed", not as "how old the oldest sample in the frame is".
namespace latency {

constexpr std::size_t kStageCount = 7;
constexpr std::size_t kWindowSize = 256;

enum class Stage : uint8_t {
    capture = 0,
    analyse = 1,
    embed = 2,
    decide = 3,
    transport = 4,
    apply = 5,
    render = 6,
};

const char* stage_name(Stage stage);

/// True for stages measured on this device rather than reported by the peer.
bool is_local_stage(Stage stage);

/// One stage's distribution over the rolling window.
struct StageStats {
    std::size_t samples = 0;
    float min_ms = 0.0f;
    float p50_ms = 0.0f;
    float p95_ms = 0.0f;
    float max_ms = 0.0f;
    float mean_ms = 0.0f;
};

/// Nearest-rank percentile over a sorted array. `fraction` is in [0, 1].
/// Nearest rank rather than interpolation so every reported figure is an actual
/// observation, which matters when the point is to quote a worst case.
float nearest_rank_percentile(const float* sorted, std::size_t count, float fraction);

class LatencyProfiler {
public:
    /// Record one observation for a stage. Non-finite and negative values are rejected
    /// rather than clamped: silently accepting a bad measurement would corrupt the
    /// percentile without any visible symptom.
    void record(Stage stage, float ms);

    /// Record a peer-reported value (transport round trip, host apply, device render).
    void record_peer_latency(Stage stage, float ms);

    StageStats stats(Stage stage) const;

    /// Sum of the medians across the stages the caller names, for the headline figure.
    float total_p50_ms() const;
    float total_p95_ms() const;

    std::size_t samples(Stage stage) const;
    void reset();

    /// Number of recordings rejected because the value was not a usable duration.
    std::size_t rejected() const { return rejected_; }

private:
    /// Ring buffer per stage. A fixed window keeps the memory footprint predictable and
    /// bounds the recomputation cost of a percentile query.
    std::array<std::array<float, kWindowSize>, kStageCount> samples_{};
    std::array<std::size_t, kStageCount> count_{};
    std::array<std::size_t, kStageCount> next_{};
    std::size_t rejected_ = 0;
};

/// Tracks the cost of the instrumentation itself.
///
/// A profiler that perturbs the thing it measures is worse than no profiler. This keeps
/// the measured overhead and compares it against the frame budget so a regression fails
/// loudly instead of quietly shifting every latency figure.
class ProfilerOverhead {
public:
    explicit ProfilerOverhead(float frame_budget_ms) : frame_budget_ms_(frame_budget_ms) {}

    void record(float overhead_ms);
    float mean_ms() const;
    float max_ms() const;
    /// Share of the frame budget consumed by instrumentation, in [0, 1].
    float budget_share() const;
    /// True when overhead has stayed under `limit_share` of the budget.
    bool within_budget(float limit_share = 0.01f) const;

private:
    float frame_budget_ms_;
    double sum_ms_ = 0.0;
    float max_ms_ = 0.0f;
    std::size_t count_ = 0;
};

}  // namespace latency
