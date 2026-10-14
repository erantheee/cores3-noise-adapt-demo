#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

/// Target-speaker gating from speaker embeddings.
///
/// This is the "who is talking" half of the voiceprint route described in
/// docs/edge-feasibility.md. It deliberately contains no model: the caller supplies an
/// embedding per frame, and this decides whether that embedding is the enrolled speaker.
/// Keeping the decision separate from the model is what makes it testable without a device
/// and what lets the model be swapped without touching the policy.
///
/// Three mechanisms are required together, and dropping any one of them is a known way to
/// fail. The sibling project measured 24-band spectral envelopes and found within-speaker
/// jitter (about 0.15) larger than the between-speaker gap (about 0.05), so no fixed
/// threshold could work:
///
///   1. The prototype is updated with an exponential moving average rather than pinned.
///      A voiceprint drifts with a cold, with distance, with speaking effort.
///   2. The acceptance threshold adapts per speaker from that speaker's own observed
///      score distribution. A global constant is what failed before.
///   3. Acceptance requires a margin over the runner-up as well as a score over the
///      threshold. "Sounds like someone" is not "sounds like this one".
///
/// There is also an explicit self-echo path. Worn on the ear, the device hears its own
/// wearer strongly, and without a separate own-voice prototype the wearer gets admitted as
/// the target.
namespace voiceprint {

/// Embedding dimensionality. ECAPA-TDNN style models produce 192 or 512; 192 is the
/// smaller of the two common sizes.
constexpr std::size_t kEmbeddingSize = 192;

using Embedding = std::array<float, kEmbeddingSize>;

/// How the current frame relates to the enrolled speaker.
enum class GateState : uint8_t {
    /// Nothing enrolled yet, so every frame is unknown.
    unenrolled,
    /// Enough frames to make a decision, and this one is the enrolled speaker.
    confirmed,
    /// Enough frames to make a decision, and this one is a different speaker.
    rejected,
    /// The frame is uncertain: near the threshold, or lacking margin over the runner-up.
    uncertain,
};

const char* gate_state_name(GateState state);

struct GateConfig {
    /// Frames of clean speech absorbed into the prototype before any decision is made.
    /// Until this many frames have arrived the gate reports `unenrolled`, because a
    /// prototype built from one or two frames is a sample of one person on one day, and
    /// deciding from it is how a gate starts rejecting its own speaker on the next.
    std::size_t enroll_frames = 8;
    /// Weight of a new frame when updating the prototype. Low enough that one bad frame
    /// cannot drag the prototype, high enough to follow a cold over a session.
    float adaptation_rate = 0.05f;
    /// Updates to the observed-score statistics before the adaptive threshold is trusted.
    /// Until then the gate uses `initial_threshold`, because deriving a threshold from
    /// three observations is worse than using a conservative constant.
    std::size_t threshold_warmup = 20;
    /// Threshold used until the observed-score statistics are warm.
    ///
    /// CALIBRATION ITEM, shared with `min_threshold` below: this is the boundary the
    /// separability measurement says separates the speaker from other people. It is the
    /// one constant that cannot be derived from observations, because deriving the
    /// separation boundary from the speaker alone is exactly the mistake the sibling
    /// project made when it measured within-speaker jitter (0.15) larger than the
    /// between-speaker gap (0.05).
    float initial_threshold = 0.75f;
    /// Rate at which the score floor falls while frames are being rejected.
    ///
    /// Without this the gate has no way back. The floor only updates on accepted frames, so
    /// a speaker whose voice moves away from the enrolment reference is rejected forever,
    /// with no amount of evidence able to bring them back. At 2e-3 per frame a gap of 0.08
    /// closes after about 40 frames, so returning to the conversation takes a couple of
    /// seconds of speech rather than a restart.
    ///
    /// It is deliberately slow: an intermittent impostor should not be able to walk the
    /// floor down and in.
    float floor_decay_per_reject = 2.0e-3f;

    /// Frames of history the score floor looks back over on the accepted path. The floor is
    /// the minimum over this window, so it recovers on its own as old low scores age out
    /// instead of needing a separate upward adaptation rate.
    std::size_t floor_window = 64;

    /// Extra headroom kept below the lowest score this speaker has actually produced.
    ///
    /// The threshold is derived from a running estimate of that floor rather than from an
    /// absolute constant. Two earlier revisions were wrong in instructive ways:
    ///
    ///   * A pure quantile of accepted scores, floored at the calibrated value: feeding a
    ///     perfectly consistent speaker drove the threshold up to the ceiling, after which
    ///     a moderate drift was rejected. The better the enrolment, the more brittle the
    ///     gate.
    ///   * A fixed margin below the typical score: the margin then had to cover the whole
    ///     within-speaker spread, which differs per speaker, so any single value is either
    ///     too tight for one and too loose for another.
    ///
    /// Tracking the observed floor scales with the speaker. Make this small; it exists to
    /// absorb measurement noise, not to add tolerance.
    float threshold_headroom = 0.02f;
    /// The adaptive threshold never drops below this, however far the voice drifts and for
    /// however long. Same value as `initial_threshold` by default: both are the measured
    /// separation boundary, and letting the floor decay past it would mean sustained
    /// evidence from the wrong person eventually opens the gate.
    float min_threshold = 0.75f;
    /// The adaptive threshold never rises above this.
    float max_threshold = 0.95f;
    /// Required margin of the best score over the runner-up when the caller supplies a
    /// second candidate. Ignored for single-speaker comparisons.
    float margin_over_runner_up = 0.05f;
    /// Similarity to own voice above which a frame is treated as the wearer regardless of
    /// how well it matches the target.
    float self_voice_rejection = 0.80f;
};

/// Outcome of feeding one frame.
struct GateDecision {
    GateState state = GateState::unenrolled;
    /// Similarity to the enrolled speaker, or 0 when nothing is enrolled.
    float similarity = 0.0f;
    /// Similarity to the wearer's own voice, or 0 when no own-voice prototype exists.
    float self_similarity = 0.0f;
    /// Threshold actually applied to this frame.
    float threshold = 0.0f;
    /// True when the wearer's own voice produced the rejection.
    bool blocked_by_self_voice = false;
    /// True when the frame scored over threshold but lacked margin over the runner-up.
    bool blocked_by_margin = false;
};

/// Cosine similarity, with both vectors treated as non-normalised.
/// Returns 0 when either vector is degenerate, rather than a NaN. L2-normalises.
void normalise(Embedding& embedding);

/// Weighted blend used for prototype updates: `target = (1-a) * target + a * sample`.
void blend(Embedding& target, const Embedding& sample, float alpha);

/// Cosine similarity in [-1, 1]; 0 for a degenerate input.
float cosine_similarity(const Embedding& a, const Embedding& b);

class TargetSpeakerGate {
public:
    explicit TargetSpeakerGate(const GateConfig& config = GateConfig{});

    /// Feed one embedding of confirmed speech.
    ///
    /// `runner_up` is the similarity to the next-best known speaker, or a negative value
    /// when the caller has no second candidate. Supplying it enables the margin check,
    /// which is the difference between "this sounds like a person" and "this sounds like
    /// the person I enrolled".
    GateDecision update(const Embedding& embedding, float runner_up = -1.0f);

    /// Begin or restart enrolment, discarding any previous prototype.
    void begin_enrollment();

    /// Register the wearer's own voice. Separate from the target prototype: the wearer is
    /// heard constantly when the device is worn, and must be distinguishable from a
    /// conversation partner.
    void set_own_voice(const Embedding& embedding);

    /// Number of frames absorbed into the prototype so far.
    std::size_t enrolled_frames() const { return enrolled_frames_; }

    /// Frames observed since enrolment completed, for the warm-up gate.
    std::size_t observed_scores() const { return observed_scores_; }

    /// Current adaptive threshold. Exposed so the panel can show it: a threshold that
    /// silently drifts is how a gate starts rejecting its own speaker.
    float threshold() const { return threshold_; }

    /// True once enough frames have been absorbed to make confident decisions.
    bool is_enrolled() const { return enrolled_frames_ >= config_.enroll_frames; }

    /// True as soon as any frame has been absorbed; the prototype exists but is not yet
    /// trustworthy. Kept separate from is_enrolled() so callers cannot accidentally treat
    /// a one-frame prototype as ready.
    bool has_prototype() const { return enrolled_frames_ > 0; }
    bool has_own_voice() const { return has_own_voice_; }

    const Embedding& prototype() const { return prototype_; }

    /// The frozen reference the operating threshold is derived from. See the note on
    /// `reference_` in the implementation: it does not adapt, deliberately.
    const Embedding& reference() const { return reference_; }

private:
    GateConfig config_;
    Embedding prototype_{};
    Embedding own_voice_{};
    bool has_own_voice_ = false;
    std::size_t enrolled_frames_ = 0;
    std::size_t observed_scores_ = 0;
    float threshold_ = 0.0f;
    /// Running quantile estimate of accepted scores, updated incrementally. A full
    /// histogram would need storage and sorting per frame; this converges to the same
    /// place and costs one multiply-add.
    float typical_score_ = 0.0f;
    /// Running estimate of the lowest score this speaker produces. The threshold is kept
    /// below it so the gate cannot reject a score it has already accepted, which is the
    /// failure mode that made an earlier revision reject its own enrolled speaker after he
    /// happened to score well during enrolment.
    float accepted_floor_ = 1.0f;
    bool floor_initialised_ = false;
    /// Recent reference scores. The floor is their minimum; see `floor_window`.
    std::array<float, 256> floor_history_{};
    std::size_t floor_count_ = 0;
    std::size_t floor_next_ = 0;
    /// Frozen copy of the prototype taken when enrolment completes, used for the scores
    /// that drive the threshold.
    ///
    /// This is the fix for a feedback loop that took three attempts to see: if the same
    /// adaptive prototype is used both for scoring and for updating, then every accepted
    /// frame pulls the prototype towards the incoming voice, which raises the score of the
    /// next frame, which pulls the floor up, which pushes the threshold to its ceiling,
    /// after which any drift is rejected. Scores measured against a moving reference are
    /// not measurements of anything.
    Embedding reference_{};
};

}  // namespace voiceprint
