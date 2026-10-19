#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>
#include "../main/controller.hpp"

namespace {
int passed = 0;
int failed = 0;
void check(bool condition, const char* name) {
    if (condition) { ++passed; std::cout << "PASS " << name << '\n'; }
    else { ++failed; std::cout << "FAIL " << name << '\n'; }
}
AnalysisFrame frame(float dbfs, float voice, bool valid = true) {
    AnalysisFrame value;
    value.timestamp_ms = 1000;
    value.broadband_dbfs = dbfs;
    value.voice_likelihood = voice;
    value.mic_valid = valid;
    value.band_dbfs.fill(dbfs);
    return value;
}
/// The bands the controller is allowed to lift. Kept here rather than read from the
/// implementation so a silent change to the tuning width shows up as a test failure.
constexpr std::size_t kSpeechBandFirst = 8;
constexpr std::size_t kSpeechBandLast = 16;
}

int main() {
    AdaptController controller;
    auto quiet = controller.update(frame(-65, 0), 150);
    check(quiet.speech_gain_db == 0, "quiet stays flat");
    check(quiet.media_percent == 100, "quiet preserves media");

    EarbudCommand steady;
    for (int i = 0; i < 12; ++i) steady = controller.update(frame(-38, 0.0f), 150);
    check(steady.tuning_mode == TuningMode::song_eq, "steady noise selects song EQ");
    check(steady.media_percent == 100, "song EQ does not silently duck media");

    EarbudCommand voice;
    for (int i = 0; i < 12; ++i) voice = controller.update(frame(-28, 0.8f), 150);
    check(voice.tuning_mode == TuningMode::external_voice_intent,
          "nearby speech selects external voice intent");
    check(voice.external_voice_gain_db > 3, "external voice intent carries bounded gain");
    check(voice.external_voice_gain_db <= kDemoSpeechGainDb, "voice gain stays under the demo ceiling");
    check(voice.external_voice_gain_db == voice.speech_gain_db,
          "external voice gain is the voice lift, not something else");

    // 外部人声增强与“我自己开口”是两条互斥路径：这里没有自身语音证据，所以只抬外部人声
    // EQ，不应触发对话感知的媒体下压。
    check(steady.media_percent == 100, "song EQ does not duck media");
    check(voice.media_percent == 100, "external voice alone does not impersonate own voice");

    // The demo has to be visible: the swing from a quiet frame to clear speech must be wide
    // enough to see on the panel and hear in the room. This is the assertion that would have
    // caught the original 5.4 dB ceiling looking like "nothing is happening".
    check(voice.speech_gain_db - quiet.speech_gain_db > 5.0f,
          "gain swing is wide enough to be visible");

    // The 24-band curve must move with the scalar in this mode, or the panel shows a flat line
    // while the scalar claims 5 dB and the two disagree. A gentle taper on the outer speech bands
    // keeps it readable as a hump rather than a rectangle.
    std::size_t lifted = 0;
    bool matches_scalar = true;
    bool outside_is_flat = true;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        const bool in_band = band >= kSpeechBandFirst && band <= kSpeechBandLast;
        if (in_band) {
            ++lifted;
            const float expected = band == kSpeechBandFirst || band == kSpeechBandLast
                                       ? voice.speech_gain_db * 0.75f
                                       : voice.speech_gain_db;
            if (std::fabs(voice.eq_gain_db[band] - expected) > 0.05f) matches_scalar = false;
        } else if (voice.eq_gain_db[band] != 0.0f) {
            outside_is_flat = false;
        }
    }
    check(lifted == kSpeechBandLast - kSpeechBandFirst + 1, "test inspects the full speech band");
    check(matches_scalar, "every speech band tracks the reported voice gain");
    check(outside_is_flat, "bands outside the speech band stay flat");

    bool song_eq_has_gain = false;
    for (float gain : steady.eq_gain_db) song_eq_has_gain = song_eq_has_gain || gain > 0.05f;
    check(song_eq_has_gain, "steady environmental masking produces a song EQ suggestion");

    // No noise reduction anywhere: the product only has gain and tuning. This is a
    // compile-time guarantee now (the field is gone from EarbudCommand); the check that
    // matters at runtime is that the flat/quiet case does not lift anything.
    check(quiet.eq_gain_db[kSpeechBandFirst] == 0, "quiet lifts no band");

    EarbudCommand failed_frame;
    for (int i = 0; i < 3; ++i) failed_frame = controller.update(frame(-20, 0, false), 100);
    check(failed_frame.state == AdaptState::mic_fault, "persistent invalid mic enters fault");
    check(failed_frame.speech_gain_db < voice.speech_gain_db, "fault fails toward zero gain");
    check(failed_frame.media_percent == 100,
          "fault keeps the media bed restored when no own voice was present");
    check(failed_frame.eq_gain_db[kSpeechBandFirst] <
              steady.eq_gain_db[kSpeechBandFirst],
          "fault flattens the tuning");

    AdaptController safety_controller;
    EarbudCommand loud;
    for (int i = 0; i < 8; ++i) loud = safety_controller.update(frame(-20, 0.0f), 150);
    check(loud.tuning_mode == TuningMode::safety_hold, "very loud non-speech selects safety hold");
    bool safety_flat = true;
    for (float gain : loud.eq_gain_db) safety_flat = safety_flat && gain == 0.0f;
    check(safety_flat, "safety hold forbids positive EQ gain");

    // The project has a standing rule: no test or demo parameter may exceed +10 dB of gain.
    // The demo exaggeration is the most likely place to break it, so it is asserted rather than
    // trusted to code review.
    check(kDemoSpeechGainDb <= 10.0f, "demo gain stays inside the project-wide 10 dB ceiling");
    check(kDemoSpeechGainDb > kMaxSpeechGainDb,
          "the demo ceiling is deliberately above the product policy");
    {
        AdaptController loud_voice;
        EarbudCommand worst;
        for (int i = 0; i < 40; ++i) worst = loud_voice.update(frame(-20, 1.0f), 150);
        check(worst.speech_gain_db <= 10.0f, "no frame may exceed the 10 dB ceiling");
        for (std::size_t band = 0; band < kBandCount; ++band) {
            check(worst.eq_gain_db[band] <= 10.0f, "no EQ band may exceed the 10 dB ceiling");
        }
    }

    // 证据策略三态。这一段是 2026-10-16 的真机数据逼出来的：
    //   * 骨传导判据被本仓库自己的数据否定（docs/apple-conversation-awareness.md 第 13 节）；
    //   * 近场代理只知道"有人说话"（第 14 节），所以它只能作为**显式的演示模式**，
    //     而且遥测必须如实报 near_field；
    //   * 默认 bone_only 是 fail-closed：没有证据就不动作。
    {
        AdaptController no_evidence;   // 默认策略 = bone_only
        EarbudCommand without_evidence;
        for (int i = 0; i < 20; ++i) without_evidence = no_evidence.update(frame(-28, 0.8f), 150);
        check(without_evidence.media_percent == 100,
              "bone_only with no evidence does not duck (fail-closed)");
        check(without_evidence.conversation != ConversationState::likely,
              "no evidence never reaches the engaged conversation state");
        check(without_evidence.own_voice.bone == false && without_evidence.own_voice.proxy == false,
              "fail-closed reports neither bone nor proxy evidence");
        check(std::string(evidence_policy_name(without_evidence.evidence_policy)) == "bone_only",
              "the frame carries the policy that produced it");

        AdaptController demo;
        demo.set_evidence_policy(EvidencePolicy::near_field_demo);
        EarbudCommand with_demo;
        for (int i = 0; i < 20; ++i) with_demo = demo.update(frame(-28, 0.8f), 150);
        check(with_demo.media_percent <= 61, "near_field_demo ducks to the moderate floor");
        check(with_demo.own_voice.proxy && !with_demo.own_voice.bone,
              "near_field_demo never claims bone evidence");
        check(with_demo.tuning_mode == TuningMode::flat,
              "own voice does not enter external voice EQ mode");
        bool own_voice_eq_is_flat = true;
        for (float gain : with_demo.eq_gain_db) own_voice_eq_is_flat &= std::abs(gain) < 0.001f;
        check(own_voice_eq_is_flat, "own voice leaves every EQ band flat");

        AdaptController off;
        off.set_evidence_policy(EvidencePolicy::disabled);
        EarbudCommand with_disabled;
        for (int i = 0; i < 20; ++i) with_disabled = off.update(frame(-28, 0.8f), 150);
        check(with_disabled.media_percent == 100, "disabled never ducks");

        // 骨传导证据在 bone_only 下必须被采用（判据本身另在 bone_voice_test 里测）。
        AdaptController bone;
        OwnVoiceEvidence evidence;
        evidence.bone = true;
        evidence.imu_ok = true;
        evidence.gravity = 9.81f;
        evidence.confidence = 0.9f;
        bone.set_own_voice_evidence(evidence);
        EarbudCommand with_bone;
        for (int i = 0; i < 20; ++i) with_bone = bone.update(frame(-28, 0.8f), 150);
        check(with_bone.media_percent <= 61, "bone_only ducks when bone evidence is present");
        check(with_bone.tuning_mode == TuningMode::flat,
              "bone-confirmed own voice does not trigger external voice EQ");
    }

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
