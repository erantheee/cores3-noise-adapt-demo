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

bool all_bands_flat(const EarbudCommand& command) {
    for (float gain : command.eq_gain_db) {
        if (std::fabs(gain) > 0.001f) return false;
    }
    return true;
}
}  // namespace

int main() {
    AdaptController controller;
    auto quiet = controller.update(frame(-65, 0), 150);
    check(quiet.speech_gain_db == 0, "quiet stays flat");
    check(quiet.media_percent == 100, "quiet preserves media");
    check(!quiet.ambient_open, "quiet keeps the environment closed");

    EarbudCommand steady;
    for (int i = 0; i < 12; ++i) steady = controller.update(frame(-38, 0.0f), 150);
    check(steady.tuning_mode == TuningMode::song_eq, "steady noise selects song EQ");
    check(steady.media_percent == 100, "song EQ does not silently duck media");
    bool song_eq_has_gain = false;
    for (float gain : steady.eq_gain_db) song_eq_has_gain = song_eq_has_gain || gain > 0.05f;
    check(song_eq_has_gain, "steady environmental masking produces a song EQ suggestion");

    // ---- 预判：第一帧就压，环境声等到确认后才开 ----
    {
        AdaptController predictor;
        // -50 dBFS：比近场代理的门限（-45）更低，所以 own_voice 证据为 0；但对话 cue 的
        // 门限是 -58 起，仍应触发预判。这条就是"预判不依赖我开口"的最小证明。
        auto first = predictor.update(frame(-50, 0.8f), 150);
        check(first.conversation == ConversationState::anticipating,
              "a nearby voice starts anticipating on the first frame");
        check(first.media_percent < 90, "anticipation ducks the media before I speak");
        check(first.media_percent >= 69, "anticipation preview stays above its bounded floor");
        check(!first.ambient_open, "anticipation does not open the environment yet");
        check(first.own_voice.confidence == 0.0f,
              "the quiet nearby voice produced no own-voice evidence");
        check(all_bands_flat(first), "anticipation leaves the song EQ flat");

        EarbudCommand engaged = first;
        for (int i = 0; i < 12; ++i) engaged = predictor.update(frame(-50, 0.8f), 150);
        check(engaged.conversation == ConversationState::likely,
              "sustained nearby voice confirms the conversation");
        check(engaged.ambient_open, "confirmed conversation asks for the environment to open");
        check(engaged.media_percent <= 61, "confirmed conversation ducks to the floor");
        check(engaged.tuning_mode == TuningMode::flat, "a conversation never lifts the song EQ");
        check(all_bands_flat(engaged), "every EQ band stays flat during a conversation");
        check(engaged.external_voice_gain_db == 0,
              "no fake 'real-voice gain' is reported during a conversation");
    }

    // ---- 对话预测不依赖自身语音证据（旧实现"反应太慢"的根因）----
    {
        AdaptController bone_only;  // 默认策略，且没有塞任何 own-voice 证据
        EarbudCommand out;
        for (int i = 0; i < 20; ++i) out = bone_only.update(frame(-45, 0.8f), 150);
        check(out.media_percent <= 61,
              "conversation prediction ducks without own-voice evidence (fail-closed no longer blocks prediction)");
        check(out.conversation == ConversationState::likely,
              "prediction reaches the conversation state on cue alone");
        check(out.own_voice.bone == false && out.own_voice.proxy == false,
              "the frame still reports no own-voice evidence");
    }

    // ---- 高噪声安全保持 ----
    {
        AdaptController safety_controller;
        EarbudCommand loud;
        for (int i = 0; i < 8; ++i) loud = safety_controller.update(frame(-20, 0.0f), 150);
        check(loud.tuning_mode == TuningMode::safety_hold, "very loud non-speech selects safety hold");
        check(all_bands_flat(loud), "safety hold forbids positive EQ gain");
        check(loud.media_percent == 100, "safety hold does not duck without a conversation");
    }

    // ---- 麦克风故障：必须复位对话状态、关环境声、把媒体抬回来 ----
    {
        AdaptController failing;
        for (int i = 0; i < 20; ++i) failing.update(frame(-45, 0.8f), 150);
        EarbudCommand failed_frame;
        for (int i = 0; i < 3; ++i) failed_frame = failing.update(frame(-20, 0, false), 100);
        check(failed_frame.state == AdaptState::mic_fault, "persistent invalid mic enters fault");
        check(failed_frame.media_percent > 90, "fault starts restoring the media bed");
        check(!failed_frame.ambient_open, "fault closes the environment");
        check(all_bands_flat(failed_frame), "fault flattens the tuning");
        for (int i = 0; i < 8; ++i) failed_frame = failing.update(frame(-20, 0, false), 100);
        check(failed_frame.media_percent == 100, "fault restores the media bed fully");
    }

    // ---- 10 dB 总天花板 ----
    check(kDemoSpeechGainDb <= 10.0f, "demo gain stays inside the project-wide 10 dB ceiling");
    check(kDemoSpeechGainDb > kMaxSpeechGainDb,
          "the demo ceiling is deliberately above the product policy");

    // ---- 证据策略只影响 own_voice 的如实上报，不影响预测 ----
    {
        AdaptController demo;
        demo.set_evidence_policy(EvidencePolicy::near_field_demo);
        EarbudCommand with_demo;
        for (int i = 0; i < 20; ++i) with_demo = demo.update(frame(-28, 0.8f), 150);
        check(with_demo.media_percent <= 61, "near_field_demo also ducks");
        check(with_demo.own_voice.proxy && !with_demo.own_voice.bone,
              "near_field_demo never claims bone evidence");
        check(with_demo.tuning_mode == TuningMode::flat,
              "conversation mode does not lift the song EQ");

        AdaptController off;
        off.set_evidence_policy(EvidencePolicy::disabled);
        EarbudCommand with_disabled;
        for (int i = 0; i < 20; ++i) with_disabled = off.update(frame(-28, 0.8f), 150);
        check(with_disabled.media_percent <= 61,
              "disabled own-voice evidence does not disable conversation prediction");
        check(with_disabled.own_voice.bone == false && with_disabled.own_voice.proxy == false,
              "disabled reports no own-voice evidence");

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
    }

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    check(std::string(conversation_state_name(ConversationState::anticipating)) == "ANTICIPATING",
          "the prediction state has a stable protocol name");
    check(EarbudCommand{}.schema_version == 4, "v4 carries the ambient_open intent");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
