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
///   1 analyse      24-band analysis for one frame (a SLICE OF stage 0, not additional time)
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

    /// Sum of the per-stage medians.
    ///
    /// NOT an end-to-end latency, and it must not be quoted as one. Stage 0 is the whole
    /// loop period while stage 1 is a slice of that same period, so the sum counts the
    /// analysis twice. It is kept because it makes a rising total obvious at a glance; the
    /// per-stage numbers are what get reported.
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

/// Tracks the cost of the instrumentation itself, in microseconds.
///
/// A profiler that perturbs the thing it measures is worse than no profiler, so the cost
/// of the probes is reported alongside the measurements they produce.
///
/// The unit is microseconds rather than milliseconds, and there is deliberately no
/// "percentage of frame budget" here. An earlier version compared the figure against a
/// fixed percentage of the control period and reported thousands of microseconds, which was
/// a true statement about a number that measured the wrong thing. What the probes actually
/// cost is a handful of microseconds; anything larger means the measurement code is doing
/// work, not that the budget is tight.
class ProfilerOverhead {
public:
    void record(float overhead_us);
    float mean_us() const;
    float max_us() const;
    std::size_t samples() const { return count_; }

private:
    double sum_us_ = 0.0;
    float max_us_ = 0.0f;
    std::size_t count_ = 0;
};

}  // namespace latency
