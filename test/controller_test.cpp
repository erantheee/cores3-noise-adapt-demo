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
    // 下压现在需要证据。这个套件测的是"有证据时状态机与调压怎么走"，所以显式给骨传导证据；
    // "没有证据就不下压"（fail-closed）由本文件末尾那条独立断言覆盖。
    const auto give_bone_evidence = [&controller](float confidence) {
        OwnVoiceEvidence evidence;
        evidence.bone = true;
        evidence.imu_ok = true;
        evidence.gravity = 9.81f;
        evidence.confidence = confidence;
        evidence.correlation = 0.9f;
        controller.set_own_voice_evidence(evidence);
    };
    auto quiet = controller.update(frame(-65, 0), 150);
    check(quiet.speech_gain_db == 0, "quiet stays flat");
    check(quiet.media_percent == 100, "quiet preserves media");

    EarbudCommand steady;
    for (int i = 0; i < 12; ++i) steady = controller.update(frame(-38, 0.0f), 150);
    check(steady.tuning_mode == TuningMode::song_eq, "steady noise selects song EQ");
    check(steady.media_percent == 100, "song EQ does not silently duck media");

    give_bone_evidence(0.9f);
    EarbudCommand voice;
    for (int i = 0; i < 12; ++i) voice = controller.update(frame(-28, 0.8f), 150);
    check(voice.tuning_mode == TuningMode::external_voice_intent,
          "nearby speech selects external voice intent");
    check(voice.external_voice_gain_db > 3, "external voice intent carries bounded gain");
    check(voice.external_voice_gain_db <= kDemoSpeechGainDb, "voice gain stays under the demo ceiling");
    check(voice.external_voice_gain_db == voice.speech_gain_db,
          "external voice gain is the voice lift, not something else");

    // Media ducking and voice lift are two separate fields and must not be conflated: the
    // external-voice number is the lift only. Ducking is its own deliberate action, and for the
    // demo it is what makes the lift noticeable -- raising a band over an unmoving bed is easy to
    // miss. The sibling OWS demo's Beat 1 ships with the same 25% floor.
    check(steady.media_percent == 100, "song EQ does not duck media");
    check(voice.media_percent < 60, "external voice intent ducks the media bed");
    check(voice.media_percent >= static_cast<int>(kDemoMediaFloorPercent) - 1,
          "media ducking respects the floor");

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
    check(failed_frame.media_percent > voice.media_percent,
          "fault restores the media bed it had ducked");
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

    // 没有证据就不下压（fail-closed）。这条断言是 2026-10-16 的近场代理实测逼出来的：
    // 代理在设备自己录的静音素材上 35/75 拍误判并压到 25%，所以"证据没来"时宁可什么都不做。
    // 没有它，IMU 挂掉会静默退化成"音乐时不时无故被压"，而那是最难查的一类故障。
    {
        AdaptController no_evidence;
        EarbudCommand without_evidence;
        for (int i = 0; i < 20; ++i) without_evidence = no_evidence.update(frame(-28, 0.8f), 150);
        check(without_evidence.media_percent == 100,
              "clear speech with no own-voice evidence does not duck (fail-closed)");
        check(without_evidence.conversation != ConversationState::likely,
              "no evidence never reaches the engaged conversation state");

        no_evidence.set_allow_near_field_fallback(true);
        EarbudCommand with_fallback;
        for (int i = 0; i < 20; ++i) with_fallback = no_evidence.update(frame(-28, 0.8f), 150);
        check(with_fallback.media_percent < 60,
              "the proxy fallback still exists when a caller opts in");
    }

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
