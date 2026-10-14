// Tests for the target-speaker gate.
//
// The gate's failure modes are quiet ones: admitting the wrong speaker, rejecting its own
// enrolled speaker after the voice drifts, letting one bad frame destroy the prototype, or
// letting the adaptive threshold run away until every frame passes. Each of those is a
// named case below.
//
// The embeddings here are synthetic directions, not model output. That is sufficient
// because the gate consumes similarities, not acoustics: what is being tested is the
// policy, and the policy is what has to be right before a model is attached. Whether real
// embeddings separate speakers at all is a separate question, answered by
// tools/voice_separability.py against real recordings.
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <algorithm>
#include <random>
#include <string>
#include "../main/voiceprint_gate.hpp"

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

using voiceprint::Embedding;
using voiceprint::GateState;
using voiceprint::TargetSpeakerGate;

std::mt19937 g_rng(4242);
std::normal_distribution<float> g_normal(0.0f, 1.0f);

/// A deterministic "speaker direction".
Embedding make_speaker(uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    Embedding embedding{};
    for (auto& value : embedding) value = normal(rng);
    voiceprint::normalise(embedding);
    return embedding;
}

/// The same speaker on a different day: mostly the same direction with drift.
Embedding drifted(const Embedding& base, float drift) {
    std::mt19937 rng(999);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    Embedding out{};
    for (std::size_t i = 0; i < voiceprint::kEmbeddingSize; ++i) {
        out[i] = base[i] + drift * normal(rng);
    }
    voiceprint::normalise(out);
    return out;
}


/// Build an embedding at a chosen cosine similarity to `base` with a controlled orthogonal
/// component. Random drift cannot do this: its similarity is whatever it happens to be, so
/// a test written that way ends up pinning the gate's behaviour to noise rather than to
/// policy. Similarity is the quantity the gate actually consumes, so the test constructs
/// the similarity directly.
Embedding at_similarity(const Embedding& base, float target, uint32_t seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<float> normal(0.0f, 1.0f);
    Embedding orthogonal{};
    for (auto& value : orthogonal) value = normal(rng);

    // Remove the component along `base` so the two are perpendicular. Two passes: one
    // leaves a residual around 0.03 after normalising 192 dimensions, which would show up
    // as the requested similarity being missed by that much.
    for (int pass = 0; pass < 2; ++pass) {
        const float projection = voiceprint::cosine_similarity(base, orthogonal);
        for (std::size_t i = 0; i < voiceprint::kEmbeddingSize; ++i) {
            orthogonal[i] -= projection * base[i];
        }
        voiceprint::normalise(orthogonal);
    }

    // base is unit length, orthogonal is unit length and perpendicular, so this mix has
    // cosine exactly `target` against base.
    const float clamped = std::clamp(target, -0.999f, 0.999f);
    const float along = clamped;
    const float across = std::sqrt(std::max(0.0f, 1.0f - clamped * clamped));
    Embedding out{};
    for (std::size_t i = 0; i < voiceprint::kEmbeddingSize; ++i) {
        out[i] = along * base[i] + across * orthogonal[i];
    }
    voiceprint::normalise(out);
    return out;
}

/// Check that the constructor above really hits the requested similarity.
void check_similarity_construction() {
    const Embedding base = make_speaker(7);
    bool all_close = true;
    for (float target : {0.0f, 0.5f, 0.75f, 0.85f, 0.9f, 0.97f}) {
        const float actual =
            voiceprint::cosine_similarity(base, at_similarity(base, target, 31));
        if (std::fabs(actual - target) > 0.005f) all_close = false;
    }
    check(all_close, "the similarity constructor hits its target");
}

Embedding zeros() {
    Embedding out{};
    out.fill(0.0f);
    return out;
}
}  // namespace

int main() {
    const Embedding alice = make_speaker(1);
    const Embedding bob = make_speaker(2);

    // --- Similarity basics ----------------------------------------------------------
    check(std::fabs(voiceprint::cosine_similarity(alice, alice) - 1.0f) < 1.0e-5f,
          "a vector has similarity 1 with itself");
    check(std::fabs(voiceprint::cosine_similarity(alice, bob)) < 0.35f,
          "independent speakers are not very similar");
    check(voiceprint::cosine_similarity(alice, zeros()) == 0.0f,
          "a degenerate vector yields 0 rather than NaN");
    {
        Embedding scaled = alice;
        for (auto& value : scaled) value *= 7.0f;
        check(std::fabs(voiceprint::cosine_similarity(alice, scaled) - 1.0f) < 1.0e-5f,
              "similarity is scale invariant");
    }

    // --- Enrolment builds a usable prototype ----------------------------------------
    {
        TargetSpeakerGate gate;
        check(!gate.has_prototype(), "a fresh gate has no prototype");
        check(!gate.is_enrolled(), "a fresh gate is not enrolled");

        // Decisions must not start from a one-frame prototype. Measuring similarity
        // against a single observation is how a gate ends up rejecting its own speaker as
        // soon as the voice moves.
        TargetSpeakerGate early;
        early.update(alice);
        check(early.has_prototype(), "frames accumulate into a prototype");
        check(!early.is_enrolled(), "a one-frame prototype is not ready to decide");
        check(early.update(bob).state == GateState::unenrolled,
              "a different speaker during enrolment produces no verdict, not a rejection");

        TargetSpeakerGate gate2;
        bool decided_during_enrollment = false;
        for (std::size_t i = 0; i < 7; ++i) {
            if (gate2.update(drifted(alice, 0.02f)).state != GateState::unenrolled) {
                decided_during_enrollment = true;
            }
        }
        check(!decided_during_enrollment, "no verdict is issued before the enrolment floor");
        check(!gate2.is_enrolled(), "seven frames is fewer than the default enrolment floor");

        // The eighth frame completes enrolment and the next one decides.
        gate2.update(drifted(alice, 0.02f));
        check(gate2.is_enrolled(), "the enrolment floor is reached at eight frames");
        check(gate2.update(alice).state == GateState::confirmed,
              "the enrolled speaker is confirmed once enrolment completes");
        check(gate2.update(bob).state == GateState::rejected,
              "a clearly different speaker is rejected once enrolment completes");
    }
    {
        // Enrolment averages frames rather than letting the first one dominate.
        TargetSpeakerGate gate;
        for (int i = 0; i < 8; ++i) gate.update(i % 2 == 0 ? alice : drifted(alice, 0.05f));
        check(gate.is_enrolled(), "enrolment completes with a mixed set of frames");
        check(gate.update(alice).state == GateState::confirmed,
              "the prototype still matches the enrolled speaker after mixed enrolment");
    }

    // --- A real speaker's score spread -------------------------------------------------
    //
    // The gate consumes similarities. A speaker does not produce one number: the same voice
    // scores slightly differently every frame. These frames use explicit similarities so
    // the test pins policy rather than noise.
    check_similarity_construction();
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 8; ++i) gate.update(at_similarity(alice, 0.95f, 100 + i));
        check(gate.is_enrolled(), "enrolment completes on the controlled frames");

        // A spread typical of one speaker across a session.
        for (float similarity : {0.97f, 0.93f, 0.90f, 0.88f, 0.92f, 0.95f}) {
            const auto decision = gate.update(at_similarity(alice, similarity, 200));
            check(decision.state == GateState::confirmed,
                  "the enrolled speaker is accepted at similarity " +
                      std::to_string(similarity) + " (threshold now " +
                      std::to_string(gate.threshold()) + ")");
        }
        check(gate.threshold() < 0.88f,
              "the threshold settled below the speaker's lowest accepted score");
    }
    {
        // The floor must adapt down to the speaker's worst frame rather than parking at
        // their best. This is what a fixed threshold cannot do.
        TargetSpeakerGate gate;
        for (int i = 0; i < 8; ++i) gate.update(at_similarity(alice, 0.97f, 300 + i));
        // Let the adaptive threshold warm up at the high score first. Comparing against the
        // pre-warm-up value would just be comparing a constant to an adaptive value.
        for (int i = 0; i < 60; ++i) gate.update(at_similarity(alice, 0.97f, 350));
        const float high_threshold = gate.threshold();
        for (int i = 0; i < 80; ++i) gate.update(at_similarity(alice, 0.87f, 400));
        check(gate.threshold() <= high_threshold,
              "the threshold did not rise when the speaker's scores fell (was " +
                  std::to_string(high_threshold) + ", now " +
                  std::to_string(gate.threshold()) + ")");
        check(gate.threshold() >= 0.75f,
              "the threshold never falls below the measured separation boundary");
    }
    {
        // The enrolled speaker keeps being accepted while their scores stay in range.
        TargetSpeakerGate gate;
        for (int i = 0; i < 8; ++i) gate.update(at_similarity(alice, 0.95f, 500 + i));
        int accepted = 0;
        for (int i = 0; i < 200; ++i) {
            if (gate.update(at_similarity(alice, 0.90f, 600)).state == GateState::confirmed) {
                ++accepted;
            }
        }
        check(accepted >= 190,
              "the enrolled speaker is accepted persistently (" + std::to_string(accepted) +
                  "/200)");
        check(gate.threshold() <= 0.90f,
              "the threshold stayed at or below the speaker's own score level");

        // And a clearly different speaker is still rejected after all that adaptation.
        check(gate.update(at_similarity(alice, 0.30f, 700)).state == GateState::rejected,
              "a different speaker is rejected after the gate has adapted");
    }
    {
        // Persistence: a speaker whose voice has moved must be able to come back. Without
        // the decay this is the case where the gate locks someone out permanently.
        TargetSpeakerGate gate;
        for (int i = 0; i < 8; ++i) gate.update(at_similarity(alice, 0.97f, 900 + i));
        for (int i = 0; i < 60; ++i) gate.update(at_similarity(alice, 0.97f, 950));

        const float moved = 0.90f;
        int accepted_after_move = 0;
        for (int i = 0; i < 400; ++i) {
            if (gate.update(at_similarity(alice, moved, 960)).state == GateState::confirmed) {
                ++accepted_after_move;
            }
        }
        check(accepted_after_move > 0,
              "a voice that has moved is eventually re-admitted, not locked out");
        check(gate.threshold() >= 0.75f,
              "re-admission does not push the threshold below the separation boundary");
    }
    {
        // Several speakers all scoring high must still be rejected by the margin check,
        // which is the case a threshold alone cannot handle.
        TargetSpeakerGate gate;
        for (int i = 0; i < 8; ++i) gate.update(at_similarity(alice, 0.95f, 800 + i));
        const auto contested = gate.update(at_similarity(alice, 0.95f, 900), 0.94f);
        check(contested.state == GateState::uncertain && contested.blocked_by_margin,
              "a near-tie with another speaker is uncertain even at a high score");
    }

    // --- The threshold never runs away downwards ------------------------------------
    //
    // An adaptive threshold that follows rejects downwards ends up admitting everyone.
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 20; ++i) gate.update(alice);
        for (int i = 0; i < 500; ++i) gate.update(drifted(bob, 0.0f));
        check(gate.threshold() >= 0.75f,
              "the threshold never falls below the separation boundary");
        check(gate.update(bob).state == GateState::rejected,
              "a different speaker is still rejected after many rejects");
    }

    // --- The threshold keeps a usable range -----------------------------------------
    //
    // A speaker who consistently scores very high does pull the threshold up, and that is
    // the intended behaviour: the threshold tracks the scores this speaker actually
    // produces, so a frame far below their observed range is not obviously them. The
    // ceiling stops that from becoming unreachable.
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 200; ++i) gate.update(alice);
        check(gate.threshold() <= 0.95f, "the threshold never exceeds its ceiling");
        // 0.98 rather than exactly 0.95: the comparison is strict, so a score equal to the
        // threshold is rejected. Picking a value on the boundary would make this test a
        // statement about floating-point equality rather than about the gate.
        check(gate.update(at_similarity(alice, 0.98f, 1200)).state == GateState::confirmed,
              "a score inside the speaker's observed range is still accepted");
        // Documented consequence of tracking the observed floor: a frame far outside the
        // range this speaker has produced is treated as a different voice. That is the
        // trade the adaptive floor makes, and the persistence decay below is what stops it
        // from being permanent.
        check(gate.update(at_similarity(alice, 0.70f, 1300)).state == GateState::rejected,
              "a score far below the speaker's observed range is rejected");
    }

    // --- Margin over the runner-up --------------------------------------------------
    //
    // Scoring over threshold means "a voice like the one I know". The margin is what
    // makes it "that specific voice".
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 20; ++i) gate.update(alice);
        const float similarity = voiceprint::cosine_similarity(gate.prototype(), alice);
        // Runner-up almost as close as the winner.
        const auto contestable = gate.update(alice, similarity - 0.01f);
        check(contestable.state == GateState::uncertain && contestable.blocked_by_margin,
              "a close runner-up downgrades an otherwise accepted frame to uncertain");
        // Runner-up clearly behind.
        const auto clear = gate.update(alice, similarity - 0.50f);
        check(clear.state == GateState::confirmed, "a distant runner-up does not block");
        // No runner-up supplied: the check is skipped rather than assumed to pass.
        const auto alone = gate.update(alice);
        check(alone.state == GateState::confirmed,
              "omitting the runner-up skips the margin check");
    }

    // --- Self-voice rejection -------------------------------------------------------
    //
    // Worn on the ear, the device hears its own wearer constantly. Without this the
    // wearer is admitted as the target.
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 20; ++i) gate.update(alice);
        gate.set_own_voice(bob);

        const auto own = gate.update(bob);
        check(own.state == GateState::rejected && own.blocked_by_self_voice,
              "the wearer's own voice is rejected even though it is a valid voice");
        check(own.self_similarity > own.similarity,
              "the self-voice rejection reports why it fired");

        const auto target = gate.update(alice);
        check(target.state == GateState::confirmed,
              "the target speaker is still accepted when own voice is registered");
        check(!target.blocked_by_self_voice, "the target is not mistaken for the wearer");
    }
    {
        // Own voice registered but the frame matches the target better: accept it.
        TargetSpeakerGate gate;
        for (int i = 0; i < 20; ++i) gate.update(alice);
        gate.set_own_voice(drifted(alice, 0.6f));
        check(gate.update(alice).state == GateState::confirmed,
              "a frame closer to the target than to own voice is accepted");
    }

    // --- Degenerate and hostile inputs ---------------------------------------------
    {
        TargetSpeakerGate gate;
        gate.update(alice);
        const auto decision = gate.update(zeros());
        check(decision.state == GateState::uncertain,
              "an all-zero embedding is reported uncertain, not confirmed");
        check(decision.similarity == 0.0f, "an all-zero embedding yields zero similarity");
        check(gate.update(zeros()).state != GateState::confirmed,
              "repeated degenerate frames never confirm");
    }
    {
        // A degenerate first frame must not become the prototype: every later frame would
        // then be compared against nothing.
        TargetSpeakerGate gate;
        gate.update(zeros());
        check(!gate.has_prototype(),
              "a degenerate enrolment frame does not create a prototype");
        gate.update(alice);
        check(gate.has_prototype(), "a valid frame afterwards creates the prototype");
    }
    {
        TargetSpeakerGate gate;
        Embedding nan_embedding = alice;
        nan_embedding[0] = std::nanf("");
        gate.update(alice);
        const auto decision = gate.update(nan_embedding);
        check(decision.state != GateState::confirmed,
              "an embedding containing NaN is never confirmed");
    }

    // --- One bad frame must not destroy the prototype -------------------------------
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 40; ++i) gate.update(alice);
        const Embedding before = gate.prototype();
        gate.update(drifted(bob, 0.0f), -1.0f);  // a rejected frame
        const Embedding after = gate.prototype();
        float change = 0.0f;
        for (std::size_t i = 0; i < voiceprint::kEmbeddingSize; ++i) {
            change += std::fabs(before[i] - after[i]);
        }
        check(change < 1.0e-4f, "a rejected frame does not modify the prototype");
    }

    // --- begin_enrollment resets cleanly --------------------------------------------
    {
        TargetSpeakerGate gate;
        for (int i = 0; i < 30; ++i) gate.update(alice);
        gate.set_own_voice(bob);
        gate.begin_enrollment();
        check(!gate.has_prototype(), "begin_enrollment clears the prototype");
        check(gate.enrolled_frames() == 0, "begin_enrollment clears the frame count");
        check(gate.observed_scores() == 0, "begin_enrollment clears the observed scores");
        check(gate.has_own_voice(),
              "begin_enrollment keeps the own-voice prototype, which is a property of the wearer");
        gate.update(bob);
        check(gate.update(bob).state != GateState::rejected,
              "the gate can enrol a different speaker after reset");
    }

    // --- State names are stable protocol values -------------------------------------
    check(std::string(voiceprint::gate_state_name(GateState::confirmed)) == "CONFIRMED",
          "state names are stable");
    check(std::string(voiceprint::gate_state_name(GateState::uncertain)) == "UNCERTAIN",
          "the uncertain state is named");

    std::cout << "VoiceprintGate: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
