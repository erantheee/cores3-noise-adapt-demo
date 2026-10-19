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

AnalysisFrame spectral_frame(float base_dbfs, std::size_t first, std::size_t last,
                             float dominant_dbfs) {
    AnalysisFrame value = frame(base_dbfs, 0.0f);
    value.band_dbfs.fill(base_dbfs);
    for (std::size_t band = first; band < last; ++band) value.band_dbfs[band] = dominant_dbfs;
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
    float previous_low = 0.0f;
    AdaptController smooth_controller;
    for (int i = 0; i < 8; ++i) {
        const EarbudCommand smoothed = smooth_controller.update(
            spectral_frame(-55.0f, 0, 6, (i % 2 == 0) ? -28.0f : -48.0f), 100);
        check(std::fabs(smoothed.eq_gain_db[2] - previous_low) < 1.0f,
              "per-band EQ does not jump between adjacent 100 ms frames");
        previous_low = smoothed.eq_gain_db[2];
    }

    // ---- 频谱分型：只重点补当前被遮蔽的低/中/高频 ----
    {
        AdaptController low_controller;
        EarbudCommand low;
        for (int i = 0; i < 12; ++i) {
            low = low_controller.update(spectral_frame(-55.0f, 0, 6, -32.0f), 150);
        }
        check(low.noise_spectrum == NoiseSpectrum::low, "low-frequency noise is classified LOW");
        check(low.eq_gain_db[2] > low.eq_gain_db[20] * 2.0f,
              "LOW noise focuses compensation on low bands");

        AdaptController high_controller;
        EarbudCommand high;
        for (int i = 0; i < 12; ++i) {
            high = high_controller.update(spectral_frame(-55.0f, 17, 24, -32.0f), 150);
        }
        check(high.noise_spectrum == NoiseSpectrum::high, "high-frequency noise is classified HIGH");
        check(high.eq_gain_db[20] > high.eq_gain_db[2] * 2.0f,
              "HIGH noise focuses compensation on high bands");
    }

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
        check(engaged.tuning_mode == TuningMode::flat,
              "a conversation never lifts the song EQ");
        check(all_bands_flat(engaged), "every EQ band stays flat during a conversation");
        check(engaged.external_voice_gain_db == 0,
              "no fake 'real-voice gain' is reported during a conversation");
    }

    // ---- 人声消失后：对话保持期不能再把中频抬着 ----
    {
        AdaptController holder;
        for (int i = 0; i < 12; ++i) holder.update(frame(-50, 0.8f), 150);
        EarbudCommand silent = holder.update(frame(-70, 0.0f), 150);
        check(silent.conversation != ConversationState::none,
              "人声刚消失时对话仍在保持期");
        check(all_bands_flat(silent), "对话保持期（没人声）EQ 回到全平直");
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

    // ---- 背景/别人说话（还没构成对话）：中频 EQ 要起来 ----
    {
        AdaptController background;
        EarbudCommand out;
        // -54 dBFS、voice 0.8：speech_active 成立，但对话 cue（-58 起按 16 dB 归一）
        // 只有 0.25，低于 0.5 门限 -> 不进入 anticipating，走 external_voice_intent。
        for (int i = 0; i < 12; ++i) out = background.update(frame(-54, 0.8f), 150);
        check(out.conversation == ConversationState::speech_nearby,
              "背景人声只报 nearby，不升级为对话（cue 未过门限）");
        check(out.media_percent == 100, "背景人声不压媒体");
        check(!out.ambient_open, "背景人声不开环境声");
        check(out.tuning_mode == TuningMode::external_voice_intent,
              "背景人声选择外部人声意图");
        bool mid_lifted = false;
        for (std::size_t band = 8; band <= 16; ++band) {
            if (out.eq_gain_db[band] > 0.05f) mid_lifted = true;
        }
        check(mid_lifted, "背景别人说话：1–3.6 kHz 中频 EQ 被抬起");
        check(out.eq_gain_db[0] == 0.0f && out.eq_gain_db[23] == 0.0f,
              "外部人声 EQ 只动中频段，不动两端");
    }

    // ---- 对话门槛高于 EQ 门槛：一般噪声不能把对话顶起来 ----
    {
        AdaptController noisy;
        EarbudCommand out;
        // voice 0.6、-45 dBFS：够"有人声"（EQ/场景）但不够对话（0.65）。
        for (int i = 0; i < 12; ++i) out = noisy.update(frame(-45, 0.6f), 150);
        check(out.conversation == ConversationState::speech_nearby,
              "voice 0.6 只报 nearby，不进入对话（对话门槛 0.65）");
        check(out.media_percent == 100 && !out.ambient_open,
              "voice 0.6 不压媒体、不开环境声");
        bool mid = false;
        for (std::size_t band = 8; band <= 16; ++band) {
            if (out.eq_gain_db[band] > 0.05f) mid = true;
        }
        check(mid, "voice 0.6 仍按背景人声抬中频 EQ");
    }

    // ---- 声纹判定 self：自己说话不抬 EQ、不压媒体、不开环境声 ----
    {
        AdaptController self_voice;
        EarbudCommand out;
        for (int i = 0; i < 12; ++i) {
            out = self_voice.update(frame(-45, 0.9f), 150, /*non_voice_event=*/false,
                                    /*external_self_voice=*/true);
        }
        check(out.conversation == ConversationState::none,
              "self 声纹不进入对话");
        check(!out.ambient_open, "self 声纹不开环境声");
        check(out.media_percent == 100, "self 声纹不压媒体");
        check(out.tuning_mode == TuningMode::flat, "self 声纹不进入外部人声 EQ");
        bool any_eq = false;
        for (std::size_t band = 0; band < 24; ++band) {
            if (out.eq_gain_db[band] > 0.01f) any_eq = true;
        }
        check(!any_eq, "self 声纹不得产生正增益");
    }

    // ---- 只有 other、没有 self 轮次：可做背景人声 EQ，但不能当成正在对话 ----
    {
        AdaptController other_only;
        EarbudCommand out;
        for (int i = 0; i < 12; ++i) {
            out = other_only.update(frame(-45, 0.9f), 150, false, false,
                                    /*conversation_authorized=*/false);
        }
        check(out.conversation == ConversationState::speech_nearby,
              "other-only speech remains background speech");
        check(!out.ambient_open && out.media_percent == 100,
              "other-only speech does not open ambient or duck media");
        check(out.tuning_mode == TuningMode::external_voice_intent,
              "other-only speech may still receive background voice EQ");
    }

    // ---- self 与 other 都出现：允许对话状态机开放环境声并让媒体 EQ 让位 ----
    {
        AdaptController turn_taking;
        EarbudCommand out;
        for (int i = 0; i < 12; ++i) {
            out = turn_taking.update(frame(-45, 0.9f), 150, false, false,
                                     /*conversation_authorized=*/true);
        }
        check(out.conversation == ConversationState::likely && out.ambient_open,
              "self/other turn taking authorizes conversation");
        check(out.media_percent <= 61 && all_bands_flat(out),
              "authorized conversation ducks media and yields the song EQ");
    }

    // ---- 键盘/风/撞击事件不参与对话证据 ----
    {
        AdaptController vetoed;
        EarbudCommand out;
        for (int i = 0; i < 12; ++i) {
            out = vetoed.update(frame(-45, 0.9f), 150, /*non_voice_event=*/true);
        }
        check(out.conversation == ConversationState::none,
              "键盘/风/撞击事件否决对话（即使 voice 0.9）");
        check(!out.ambient_open, "非人声事件不得开环境声");
        check(out.media_percent == 100, "非人声事件不得压媒体");
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

    {
        EarbudCommand song;
        song.tuning_mode = TuningMode::song_eq;
        song.conversation = ConversationState::none;
        for (std::size_t band = 0; band < kBandCount; ++band) song.eq_gain_db[band] = 6.0f;
        apply_wind_eq(song);
        check(song.eq_gain_db[0] == 1.5f && song.eq_gain_db[4] == 1.5f,
              "wind EQ scales the five low bands down to 1/4");
        check(song.eq_gain_db[5] == 6.0f && song.eq_gain_db[23] == 6.0f,
              "wind EQ leaves mid/high band lifts untouched");

        EarbudCommand talking = song;
        talking.conversation = ConversationState::speech_nearby;
        for (std::size_t band = 0; band < kBandCount; ++band) talking.eq_gain_db[band] = 6.0f;
        apply_wind_eq(talking);
        check(talking.eq_gain_db[0] == 6.0f,
              "speech takes priority over wind EQ (D21)");

        EarbudCommand flat;
        flat.tuning_mode = TuningMode::flat;
        for (std::size_t band = 0; band < kBandCount; ++band) flat.eq_gain_db[band] = 6.0f;
        apply_wind_eq(flat);
        check(flat.eq_gain_db[0] == 6.0f, "wind EQ does not shape non-song modes");

        EarbudCommand already_low;
        already_low.tuning_mode = TuningMode::song_eq;
        already_low.eq_gain_db[0] = 1.0f;
        apply_wind_eq(already_low);
        check(already_low.eq_gain_db[0] == 0.25f, "wind EQ reduces, never raises, a low band");
    }

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    check(std::string(conversation_state_name(ConversationState::anticipating)) == "ANTICIPATING",
          "the prediction state has a stable protocol name");
    check(EarbudCommand{}.schema_version == 4, "v4 carries the ambient_open intent");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
