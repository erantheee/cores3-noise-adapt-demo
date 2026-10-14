#include "voiceprint_gate.hpp"

#include <algorithm>
#include <cmath>

namespace voiceprint {
namespace {
float dot(const Embedding& a, const Embedding& b) {
    float sum = 0.0f;
    for (std::size_t i = 0; i < kEmbeddingSize; ++i) sum += a[i] * b[i];
    return sum;
}

float norm(const Embedding& a) { return std::sqrt(dot(a, a)); }

/// True when a value can take part in a comparison. A NaN that reached the gate would
/// make every comparison false and silently reject everything, which looks exactly like
/// "the speaker is not here" rather than like a bug.
bool usable_similarity(float value) { return std::isfinite(value); }
}  // namespace

const char* gate_state_name(GateState state) {
    switch (state) {
        case GateState::unenrolled: return "UNENROLLED";
        case GateState::confirmed: return "CONFIRMED";
        case GateState::rejected: return "REJECTED";
        case GateState::uncertain: return "UNCERTAIN";
    }
    return "UNKNOWN";
}

void normalise(Embedding& embedding) {
    const float length = norm(embedding);
    if (!(length > 1.0e-9f)) return;  // leave a degenerate vector alone; similarity returns 0
    for (float& value : embedding) value /= length;
}

void blend(Embedding& target, const Embedding& sample, float alpha) {
    const float a = std::clamp(alpha, 0.0f, 1.0f);
    for (std::size_t i = 0; i < kEmbeddingSize; ++i) {
        target[i] = (1.0f - a) * target[i] + a * sample[i];
    }
}

float cosine_similarity(const Embedding& a, const Embedding& b) {
    const float denominator = norm(a) * norm(b);
    if (!(denominator > 1.0e-9f)) return 0.0f;
    return std::clamp(dot(a, b) / denominator, -1.0f, 1.0f);
}

TargetSpeakerGate::TargetSpeakerGate(const GateConfig& config)
    : config_(config), threshold_(config.initial_threshold) {
    floor_history_.fill(1.0f);
}

void TargetSpeakerGate::begin_enrollment() {
    prototype_.fill(0.0f);
    enrolled_frames_ = 0;
    observed_scores_ = 0;
    threshold_ = config_.initial_threshold;
    typical_score_ = 0.0f;
    accepted_floor_ = 1.0f;
    floor_initialised_ = false;
    reference_.fill(0.0f);
    floor_count_ = 0;
    floor_next_ = 0;
    // Filled with 1.0 rather than left zeroed: the floor is the minimum over this window,
    // and a zero in an unwritten slot would pin the threshold to its ceiling. That is a
    // real failure mode, not a theoretical one -- it made the gate reject its own speaker
    // as the window filled.
    floor_history_.fill(1.0f);
}

void TargetSpeakerGate::set_own_voice(const Embedding& embedding) {
    own_voice_ = embedding;
    normalise(own_voice_);
    has_own_voice_ = true;
}

GateDecision TargetSpeakerGate::update(const Embedding& embedding, float runner_up) {
    GateDecision decision;

    Embedding sample = embedding;
    normalise(sample);
    // A degenerate embedding carries no identity information. Reporting it as unknown is
    // the honest answer; admitting it would poison both the prototype and the statistics.
    if (!(norm(sample) > 1.0e-9f)) {
        decision.state = has_prototype() ? GateState::uncertain : GateState::unenrolled;
        decision.threshold = threshold_;
        return decision;
    }

    // --- Enrolment ---------------------------------------------------------------
    // No decisions are made until the prototype has enough frames behind it. A prototype
    // built from one frame is a single observation of one person on one day; deciding from
    // it is how a gate ends up rejecting its own speaker as soon as the voice moves.
    if (enrolled_frames_ < config_.enroll_frames) {
        if (enrolled_frames_ == 0) {
            prototype_ = sample;
        } else {
            // Running mean: each frame weighted equally while enrolling, so the prototype
            // is not dominated by whichever frame happened to arrive first.
            blend(prototype_, sample, 1.0f / static_cast<float>(enrolled_frames_ + 1));
        }
        ++enrolled_frames_;
        normalise(prototype_);
        if (enrolled_frames_ == config_.enroll_frames) {
            // Enrolment just completed: freeze the reference and seed the floor from it.
            reference_ = prototype_;
            accepted_floor_ = 1.0f;
            floor_initialised_ = false;
            floor_count_ = 0;
            floor_next_ = 0;
        }
        decision.state = GateState::unenrolled;
        decision.threshold = threshold_;
        return decision;
    }

    // --- Similarity --------------------------------------------------------------
    const float similarity = cosine_similarity(prototype_, sample);
    decision.similarity = similarity;

    // --- Self-voice takes precedence --------------------------------------------
    // The wearer is heard constantly. Without this check the wearer is admitted as the
    // target, which in use means the device responds to its owner instead of the person
    // being spoken to.
    if (has_own_voice_) {
        const float self_similarity = cosine_similarity(own_voice_, sample);
        decision.self_similarity = self_similarity;
        if (usable_similarity(self_similarity) && self_similarity >= config_.self_voice_rejection &&
            self_similarity > similarity) {
            decision.state = GateState::rejected;
            decision.blocked_by_self_voice = true;
            decision.threshold = threshold_;
            return decision;
        }
    }

    const float applied_threshold = threshold_;
    decision.threshold = applied_threshold;

    if (similarity < applied_threshold) {
        decision.state = GateState::rejected;
        // Persistence path: sustained evidence that the speaker no longer scores where they
        // used to slowly lowers the floor, so a voice that has genuinely moved can come
        // back. Slow on purpose -- a handful of impostor frames must not open the gate.
        accepted_floor_ = std::max(config_.min_threshold,
                                   accepted_floor_ - config_.floor_decay_per_reject);
        threshold_ = std::clamp(accepted_floor_ - config_.threshold_headroom,
                                config_.min_threshold, config_.max_threshold);
        return decision;
    }

    // --- Margin over the runner-up ----------------------------------------------
    // Scoring over threshold says "this is a voice like the one I know". Requiring a gap
    // to the next-best candidate is what says "this is that specific voice".
    if (runner_up >= 0.0f && usable_similarity(runner_up)) {
        if (similarity - runner_up < config_.margin_over_runner_up) {
            decision.state = GateState::uncertain;
            decision.blocked_by_margin = true;
            return decision;
        }
    }

    // --- Accepted: adapt ----------------------------------------------------------
    typical_score_ += config_.adaptation_rate * (similarity - typical_score_);

    // The floor is measured against the FROZEN reference, never against the adaptive
    // prototype. Measured against a moving reference the score rises every time the
    // prototype chases the voice, the floor follows it up, and the threshold walks to its
    // ceiling -- which is how this code first managed to reject its own speaker.
    const float reference_score = cosine_similarity(reference_, sample);
    const std::size_t window = std::max<std::size_t>(config_.floor_window, 1);
    const std::size_t capacity = floor_history_.size();
    const std::size_t span = std::min(window, capacity);
    floor_history_[floor_next_] = reference_score;
    floor_next_ = (floor_next_ + 1) % span;
    if (floor_count_ < span) ++floor_count_;
    accepted_floor_ = floor_history_[0];
    for (std::size_t i = 1; i < floor_count_; ++i) {
        accepted_floor_ = std::min(accepted_floor_, floor_history_[i]);
    }
    floor_initialised_ = floor_count_ > 0;

    ++observed_scores_;
    if (observed_scores_ >= config_.threshold_warmup) {
        const float adaptive = accepted_floor_ - config_.threshold_headroom;
        threshold_ = std::clamp(adaptive, config_.min_threshold, config_.max_threshold);
    }

    blend(prototype_, sample, config_.adaptation_rate);
    normalise(prototype_);

    decision.state = GateState::confirmed;
    return decision;
}

}  // namespace voiceprint
