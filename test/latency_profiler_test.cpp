// Tests for the latency profiler.
//
// The point of this module is to produce numbers that get quoted to decision makers, so
// the failure modes that matter are ones that would quietly produce a *wrong* number:
// a percentile that is not an observation, a rejected sample that still shifts the
// statistics, a ring buffer that double-counts, or instrumentation that perturbs the
// thing it measures.
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>
#include "../main/latency_profiler.hpp"

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

bool close(float a, float b, float tolerance = 1.0e-4f) { return std::fabs(a - b) <= tolerance; }
}  // namespace

int main() {
    using latency::LatencyProfiler;
    using latency::ProfilerOverhead;
    using latency::Stage;

    // --- Stage classification -------------------------------------------------------
    check(latency::is_local_stage(Stage::analyse), "analyse is a local stage");
    check(latency::is_local_stage(Stage::decide), "decide is a local stage");
    check(!latency::is_local_stage(Stage::transport), "transport is a peer-reported stage");
    check(!latency::is_local_stage(Stage::render), "render is a peer-reported stage");
    check(std::string(latency::stage_name(Stage::analyse)) == "analyse",
          "stage names are stable");

    // --- Nearest-rank percentile must return an actual observation -------------------
    {
        const float values[] = {1.0f, 2.0f, 3.0f, 4.0f, 5.0f, 6.0f, 7.0f, 8.0f, 9.0f, 10.0f};
        check(close(latency::nearest_rank_percentile(values, 10, 0.5f), 5.0f),
              "p50 of 1..10 is 5");
        check(close(latency::nearest_rank_percentile(values, 10, 0.95f), 10.0f),
              "p95 of 1..10 is 10");
        check(close(latency::nearest_rank_percentile(values, 10, 0.0f), 1.0f),
              "fraction 0 returns the minimum");
        check(close(latency::nearest_rank_percentile(values, 10, 1.0f), 10.0f),
              "fraction 1 returns the maximum");
        check(close(latency::nearest_rank_percentile(nullptr, 0, 0.5f), 0.0f),
              "an empty input yields zero rather than reading out of bounds");
    }

    // --- Empty stage reports zeros, not garbage -------------------------------------
    {
        LatencyProfiler profiler;
        const auto stats = profiler.stats(Stage::analyse);
        check(stats.samples == 0 && close(stats.p50_ms, 0.0f) && close(stats.max_ms, 0.0f),
              "an unrecorded stage reports zeros");
        check(close(profiler.total_p50_ms(), 0.0f), "the total of nothing is zero");
    }

    // --- Basic statistics -----------------------------------------------------------
    {
        LatencyProfiler profiler;
        for (int i = 1; i <= 100; ++i) profiler.record(Stage::analyse, static_cast<float>(i));
        const auto stats = profiler.stats(Stage::analyse);
        check(stats.samples == 100, "all samples are recorded");
        check(close(stats.min_ms, 1.0f), "minimum is tracked");
        check(close(stats.max_ms, 100.0f), "maximum is tracked");
        check(close(stats.p50_ms, 50.0f), "p50 is the median observation");
        check(close(stats.p95_ms, 95.0f), "p95 is an observation");
        check(close(stats.mean_ms, 50.5f), "mean is computed over the window");
    }

    // --- Bad measurements are rejected, and rejection must not bias the rest --------
    {
        LatencyProfiler profiler;
        profiler.record(Stage::decide, 2.0f);
        profiler.record(Stage::decide, -1.0f);                     // negative
        profiler.record(Stage::decide, std::nanf(""));             // NaN
        profiler.record(Stage::decide, INFINITY);                  // infinite
        profiler.record(Stage::decide, 4.0f);
        const auto stats = profiler.stats(Stage::decide);
        check(stats.samples == 2, "only usable durations enter the statistics");
        check(profiler.rejected() == 3, "rejected recordings are counted");
        check(close(stats.max_ms, 4.0f), "a rejected sample does not become the maximum");
        check(close(stats.mean_ms, 3.0f), "a rejected sample does not shift the mean");
    }

    // --- The window is bounded and the oldest samples leave ------------------------
    {
        LatencyProfiler profiler;
        const std::size_t window = latency::kWindowSize;
        for (std::size_t i = 0; i < window; ++i) profiler.record(Stage::analyse, 10.0f);
        for (std::size_t i = 0; i < window; ++i) profiler.record(Stage::analyse, 20.0f);
        const auto stats = profiler.stats(Stage::analyse);
        check(stats.samples == window, "the window never exceeds its capacity");
        check(close(stats.min_ms, 20.0f), "old samples are evicted, not averaged in");
        check(close(stats.p50_ms, 20.0f), "the window reflects recent behaviour");
    }

    // --- A partially filled ring must not read uninitialised slots ------------------
    {
        LatencyProfiler profiler;
        for (int i = 0; i < 5; ++i) profiler.record(Stage::embed, 3.0f);
        const auto stats = profiler.stats(Stage::embed);
        check(stats.samples == 5, "a partial window reports its true count");
        check(close(stats.min_ms, 3.0f) && close(stats.max_ms, 3.0f),
              "a partial window does not leak zero samples in");
    }

    // --- Peer-reported stages are attributed correctly ------------------------------
    {
        LatencyProfiler profiler;
        profiler.record(Stage::analyse, 3.0f);
        profiler.record_peer_latency(Stage::transport, 40.0f);
        profiler.record_peer_latency(Stage::render, 311.0f);
        check(close(profiler.stats(Stage::analyse).p50_ms, 3.0f), "local stage is separate");
        check(close(profiler.stats(Stage::transport).p50_ms, 40.0f), "transport is recorded");
        check(close(profiler.stats(Stage::render).p50_ms, 311.0f), "render is recorded");
        check(close(profiler.total_p50_ms(), 354.0f),
              "the total sums every stage, which is what makes the breakdown necessary");
    }

    // --- Reset clears everything ----------------------------------------------------
    {
        LatencyProfiler profiler;
        profiler.record(Stage::analyse, 5.0f);
        profiler.record(Stage::decide, -1.0f);
        profiler.reset();
        check(profiler.samples(Stage::analyse) == 0, "reset clears sample counts");
        check(profiler.rejected() == 0, "reset clears the rejection counter");
        check(close(profiler.stats(Stage::decide).max_ms, 0.0f), "reset clears the values");
    }

    // --- Instrumentation overhead must be measurable ---------------------------------
    //
    // The unit is microseconds and there is no frame-budget comparison. An earlier version
    // reported milliseconds and compared them against 1% of the frame budget, which failed
    // loudly while measuring the analysis rather than the probes.
    {
        ProfilerOverhead overhead;
        for (int i = 0; i < 10; ++i) overhead.record(6.0f);
        check(close(overhead.mean_us(), 6.0f), "overhead mean is in microseconds");
        check(close(overhead.max_us(), 6.0f), "overhead max is in microseconds");
        check(overhead.samples() == 10, "overhead sample count is tracked");
    }
    {
        // Probe cost is a handful of microseconds; the report showed 5.8 ms when it was
        // measuring the analysis, which is three orders of magnitude out.
        ProfilerOverhead overhead;
        overhead.record(4.0f);
        overhead.record(7.0f);
        overhead.record(5.0f);
        check(overhead.max_us() < 100.0f,
              "probe cost stays in the microseconds range for a healthy instrument");
    }
    {
        // Bad values are rejected rather than skewing the figure.
        ProfilerOverhead overhead;
        overhead.record(5.0f);
        overhead.record(-1.0f);
        overhead.record(std::nanf(""));
        check(overhead.samples() == 1, "invalid overhead samples are ignored");
    }
    {
        ProfilerOverhead overhead;
        check(close(overhead.mean_us(), 0.0f) && close(overhead.max_us(), 0.0f),
              "an unused overhead tracker reports zeros rather than dividing by zero");
    }

    std::cout << "LatencyProfiler: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
