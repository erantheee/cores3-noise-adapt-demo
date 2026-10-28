#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>
#include "../main/controller.hpp"
#include "w1_timeline_golden.h"

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

    // ---- 内容优先的有符号曲线（D38）：凸显人声，而不是复读噪声谱 ----
    {
        AdaptController low_controller;
        EarbudCommand low;
        for (int i = 0; i < 12; ++i) {
            low = low_controller.update(spectral_frame(-38.0f, 0, 6, -30.0f), 150);
        }
        check(low.noise_spectrum == NoiseSpectrum::low, "low-frequency noise is classified LOW");
        check(low.tuning_mode == TuningMode::song_eq, "low-frequency noise still selects song EQ");
        check(low.eq_gain_db[14] > 0.5f,
              "content prior lifts the 2.5 kHz presence band even in low-frequency rumble");
        check(low.eq_gain_db[14] > low.eq_gain_db[1],
              "presence outranks the noise-dominated bass band it is competing with");
        check(low.eq_gain_db[3] < -0.05f,
              "the 346 Hz mud band is cut (signed EQ), not boosted with the noise");

        AdaptController high_controller;
        EarbudCommand high;
        for (int i = 0; i < 12; ++i) {
            high = high_controller.update(spectral_frame(-38.0f, 17, 24, -30.0f), 150);
        }
        check(high.noise_spectrum == NoiseSpectrum::high, "high-frequency noise is classified HIGH");
        check(high.eq_gain_db[21] > 0.0f,
              "air band still gets a lift when the content prior asks for it");
        check(high.eq_gain_db[21] < low.eq_gain_db[21],
              "but the air band is damped when high-frequency noise owns it");
        check(std::fabs(high.eq_gain_db[3] - low.eq_gain_db[3]) < 0.02f,
              "the de-mud cut is not damped by noise ownership (cuts are always useful)");

        // 噪声暴露单调性：越吵动作越大（形状不变）。
        AdaptController quietish;
        EarbudCommand quiet_curve;
        for (int i = 0; i < 12; ++i) {
            quiet_curve = quietish.update(frame(-52.0f, 0.0f), 150);
        }
        check(quiet_curve.eq_gain_db[14] > 0.0f && quiet_curve.eq_gain_db[14] < low.eq_gain_db[14],
              "louder noise produces a larger presence lift");
    }

    // ---- 预判：窗内累计 0.5 s 预下压、0.6 s 确认（2026-10-23 窗口累计判据）；
    //     单帧/断续噪声不动作（D39）----
    // external_self_voice 显式给 false：声纹没有 self 判定时，D16 的预判必须照常发生。
    {
        AdaptController predictor;
        // 先把 250 ms 平滑电平带到 -45 左右，模拟"房间有底噪"（现场演示不在静音房）。
        // 否则前几拍 smoothed_dbfs_ 仍在 -56 以下，模式会因为"安静"而 flat，掩盖掉
        // 下面这条断言要验证的行为（未确认预判不压平 EQ）。
        for (int i = 0; i < 10; ++i) {
            predictor.update(frame(-45, 0.0f), 100, /*non_voice_event=*/false,
                             /*external_self_voice=*/false,
                             /*keyboard_event=*/false, /*speech_event=*/false);
        }
        // -50 dBFS：比近场代理的门限（-45）更低，所以 own_voice 证据为 0；但对话 cue 的
        // 门限是 -58 起，仍应触发预判。这条就是"预判不依赖我开口"的最小证明。
        auto first = predictor.update(frame(-50, 0.8f), 100, /*non_voice_event=*/false,
                                      /*external_self_voice=*/false,
                                      /*keyboard_event=*/false, /*speech_event=*/true);
        check(first.conversation == ConversationState::speech_nearby,
              "a single nearby-voice frame does not claim a conversation yet");
        check(first.media_percent == 100, "a single cue frame leaves the media alone");
        check(first.own_voice.confidence == 0.0f,
              "the quiet nearby voice produced no own-voice evidence");

        // 累计 400 ms 证据（首帧 100 + 再 3 帧）→ 预测中，还没到 500 ms 预下压。
        EarbudCommand preview = first;
        for (int i = 0; i < 3; ++i) {
            preview = predictor.update(frame(-50, 0.8f), 100, /*non_voice_event=*/false,
                                       /*external_self_voice=*/false,
                                       /*keyboard_event=*/false, /*speech_event=*/true);
        }
        check(preview.conversation == ConversationState::speech_nearby,
              "400 ms is not yet anticipating at the controller (preview needs 500 ms)");
        check(!preview.ambient_open, "no ambient intent before confirmation");
        // 2026-10-28 贴耳打字现场修正：单帧/短瞬态不能压平 EQ。旧写法在状态机一进
        // anticipating（首帧证据）就 flat，导致敲击/音乐瞬态让 EQ 100–400 ms 闪平
        // （124 s 里 34 次，掩蔽被反复打断）。现在 EQ 压平跟着"预览确认（500 ms）"走。
        check(preview.tuning_mode == TuningMode::external_voice_intent,
              "未确认的预判不压平 EQ：背景人声先走掩蔽");

        // 再 100 ms（累计 500 ms）→ 预下压 + EQ 压平（预览确认）。
        preview = predictor.update(frame(-50, 0.8f), 100, /*non_voice_event=*/false,
                                   /*external_self_voice=*/false,
                                   /*keyboard_event=*/false, /*speech_event=*/true);
        check(preview.media_percent < 95, "500 ms of evidence ducks the media before I speak");
        check(preview.media_percent >= 69, "anticipation preview stays above its bounded floor");
        check(preview.tuning_mode == TuningMode::flat,
              "预览确认（500 ms）后 EQ 才压平");

        // 再 300 ms（累计 800 ms）→ 确认（2026-10-23：确认保持连续 800 ms，
        // 事件帧中立；见 conversation_awareness 注释）。
        EarbudCommand engaged = preview;
        for (int i = 0; i < 3; ++i) {
            engaged = predictor.update(frame(-50, 0.8f), 100, /*non_voice_event=*/false,
                                       /*external_self_voice=*/false,
                                       /*keyboard_event=*/false, /*speech_event=*/true);
        }
        check(engaged.conversation == ConversationState::likely,
              "sustained nearby voice confirms the conversation");
        check(engaged.ambient_open, "confirmed conversation asks for the environment to open");
        // 下压有一个 150 ms 时间常数，确认后再走 4 拍（400 ms）应当贴住 60% 地板。
        for (int i = 0; i < 4; ++i) {
            engaged = predictor.update(frame(-50, 0.8f), 100, /*non_voice_event=*/false,
                                       /*external_self_voice=*/false,
                                       /*keyboard_event=*/false, /*speech_event=*/true);
        }
        check(engaged.media_percent <= 61, "confirmed conversation ducks to the floor");
        check(engaged.tuning_mode == TuningMode::flat,
              "a conversation never lifts the song EQ");
        // EQ 曲线有 650/850 ms 的一阶平滑（D38）：mode 变 flat 之后 band_gain 还要几秒
        // 才收敛到 0；2026-10-28 起 EQ 从"预览确认（500 ms）"才开始压平、比旧写法晚
        // 400 ms，所以这里必须给足平滑时间，否则测到的是 slew 尾巴而不是行为。
        for (int i = 0; i < 70; ++i) {
            engaged = predictor.update(frame(-50, 0.8f), 100, /*non_voice_event=*/false,
                                       /*external_self_voice=*/false,
                                       /*keyboard_event=*/false, /*speech_event=*/true);
        }
        check(all_bands_flat(engaged), "every EQ band converges to flat during a conversation");
        // 标量增益也是 600 ms 一阶释放（D38）：从"预览阶段的背景人声强调"收敛到 0，
        // 7 s 后只剩 1e-5 量级，按容差判"没有在报现实人声增益"。
        check(std::fabs(engaged.external_voice_gain_db) < 0.01f,
              "no fake 'real-voice gain' is reported during a conversation");
    }

    // ---- W1 现场噪声（1179 帧，work/diagnosis/live-timeline.json）不得把媒体压走/
    //      开环境声（2026-10-23 用户拍板：误触率不比现状差，用数据验证）----
    {
        AdaptController fragmented;
        float lowest_media = 100.0f;
        bool any_conversation = false;
        bool any_ambient = false;
        for (int i = 0; i < kW1TimelineFrames; ++i) {
            const std::int8_t kind = kW1Timeline[i];
            const AnalysisFrame input = (kind == 1) ? frame(-45, 0.8f) : frame(-70, 0.0f);
            const EarbudCommand out = fragmented.update(
                input, 100, /*non_voice_event=*/kind == -1,
                /*external_self_voice=*/false);
            lowest_media = std::min(lowest_media, static_cast<float>(out.media_percent));
            any_conversation = any_conversation || out.conversation == ConversationState::likely;
            any_ambient = any_ambient || out.ambient_open;
        }
        check(!any_conversation, "W1 noise never confirms a conversation");
        check(!any_ambient, "W1 noise never asks for the environment");
        check(lowest_media > 60.0f,
              "W1 noise never reaches the confirmed duck floor (no 60% lock)");
    }

    // ---- 人声消失后：对话保持期不能再把中频抬着 ----
    {
        AdaptController holder;
        for (int i = 0; i < 12; ++i) holder.update(frame(-50, 0.8f), 150,
                                                   false, false, false, /*speech_event=*/true);
        EarbudCommand silent = holder.update(frame(-70, 0.0f), 150);
        check(silent.conversation != ConversationState::none,
              "人声刚消失时对话仍在保持期");
        check(all_bands_flat(silent), "对话保持期（没人声）EQ 回到全平直");
    }

    // ---- 对话预测不依赖自身语音证据（旧实现"反应太慢"的根因）----
    {
        AdaptController bone_only;  // 默认策略，且没有塞任何 own-voice 证据
        EarbudCommand out;
        for (int i = 0; i < 20; ++i) out = bone_only.update(frame(-45, 0.8f), 150,
                                                             false, false, false,
                                                             /*speech_event=*/true);
        check(out.media_percent <= 61,
              "conversation prediction ducks without own-voice evidence (fail-closed no longer blocks prediction)");
        check(out.conversation == ConversationState::likely,
              "prediction reaches the conversation state on cue alone");
        check(out.own_voice.bone == false && out.own_voice.proxy == false,
              "the frame still reports no own-voice evidence");
    }

    // ---- 背景/别人说话（还没构成对话）：内容曲线 + 人声加权 ----
    {
        AdaptController background;
        EarbudCommand out;
        // -54 dBFS、voice 0.8：speech_active 成立，但对话 cue（-58 起按 16 dB 归一）
        // 只有 0.25，低于 0.5 门限 -> 不进入 anticipating，走 external_voice_intent。
        // D54：进 EXTERNAL_VOICE_INTENT 需要 300 ms 滞回，之后还有 650 ms EQ 平滑；
        // 20 拍 × 150 ms 让曲线收敛。
        for (int i = 0; i < 20; ++i) out = background.update(frame(-54, 0.8f), 150,
                                                              false, false, false,
                                                              /*speech_event=*/true);
        check(out.conversation == ConversationState::speech_nearby,
              "背景人声只报 nearby，不升级为对话（cue 未过门限）");
        check(out.media_percent == 100, "背景人声不压媒体");
        check(!out.ambient_open, "背景人声不开环境声");
        check(out.tuning_mode == TuningMode::external_voice_intent,
              "背景人声选择外部人声意图");
        check(out.eq_gain_db[14] >= 3.0f,
              "背景别人说话：语音段被抬到接近单段上限（demo 掩蔽，D41）");
        check(out.eq_gain_db[1] >= 0.8f,
              "背景别人说话：全频段也有一层薄抬升（掩蔽，不是只抬人声）");
        check(out.eq_gain_db[14] > out.eq_gain_db[1] &&
                  out.eq_gain_db[14] > out.eq_gain_db[21],
              "外部人声意图把 1.4–4 kHz 放在低频/空气段之上");
    }

    // ---- 微小噪声在 speech_active 门限附近不得让 EQ 跳变（2026-10-22）----
    // 旧行为：voice 刚过 0.55 就把 D41 掩蔽整条 +1/+4 dB 拉满，单帧噪声也会顶起一个台阶。
    // 新行为：掩蔽深度按置信度线性连续缩放（0.55→0，0.75→满幅），不加时间去抖；
    // 这里同时钉住"不跳"和"不牺牲真实背景人声的满幅/明显度"。
    {
        AdaptController tiny;
        EarbudCommand out;
        for (int i = 0; i < 40; ++i) out = tiny.update(frame(-54.0f, 0.0f), 100);
        const float before = out.eq_gain_db[14];
        out = tiny.update(frame(-54.0f, 0.56f), 100, false, false, false,
                           /*speech_event=*/true);
        check(std::fabs(out.eq_gain_db[14] - before) < 0.05f,
              "a single frame just over the voice threshold does not step the EQ");

        for (int i = 0; i < 30; ++i) {
            out = tiny.update(frame(-54.0f, 0.56f), 100, false, false, false,
                              /*speech_event=*/true);
        }
        check(out.eq_gain_db[14] < 0.5f,
              "sustained marginal voice stays well below the full masking depth");
        check(out.media_percent == 100 && !out.ambient_open,
              "marginal voice does not change media ducking or ambient sound");

        // 0.60 是现场房间噪声 voice_likelihood 常漂到的区间；线性映射保留 25% 掩蔽，
        // 仍应明显小于满幅（0.8 的满幅测试见下），但不需要压到"听不出来"。
        AdaptController room_noise;
        EarbudCommand noise_out;
        for (int i = 0; i < 30; ++i) {
            noise_out = room_noise.update(frame(-54.0f, 0.60f), 100, false, false, false,
                                          /*speech_event=*/true);
        }
        check(noise_out.eq_gain_db[14] < 1.5f,
              "room-noise-grade voice 0.60 stays below the full masking depth");
        check(noise_out.media_percent == 100 && !noise_out.ambient_open,
              "room-noise-grade voice still does not duck media or open ambient");

        AdaptController strong;
        EarbudCommand full_mask;
        for (int i = 0; i < 20; ++i) {
            full_mask = strong.update(frame(-54.0f, 0.8f), 150, false, false, false,
                                      /*speech_event=*/true);
        }
        check(full_mask.eq_gain_db[14] >= 3.0f,
              "high-confidence background voice still reaches the full masking depth");
    }

    // ---- D54：背景人声模式滞回（2026-10-28 现场"歌曲声音忽大忽小"）----
    {
        AdaptController flicker;
        EarbudCommand out;
        // 先让平滑电平进入正常区间（-54 dBFS）。
        for (int i = 0; i < 12; ++i) out = flicker.update(frame(-54.0f, 0.0f), 100);
        // 单帧 speech：不能立刻切 EVI（1 s 事件窗漏音会让它每拍跳）。
        out = flicker.update(frame(-54.0f, 0.8f), 100, false, false, false,
                             /*speech_event=*/true);
        check(out.tuning_mode != TuningMode::external_voice_intent,
              "a single speech-like frame does not flip the mode to EVI");
        // 连续 300 ms 之后进入 EVI。
        for (int i = 0; i < 3; ++i) {
            out = flicker.update(frame(-54.0f, 0.8f), 100, false, false, false,
                                 /*speech_event=*/true);
        }
        check(out.tuning_mode == TuningMode::external_voice_intent,
              "300 ms of sustained voice enters EVI (hysteresis)");
        // 单帧静音也不能立刻退出。
        out = flicker.update(frame(-54.0f, 0.0f), 100);
        check(out.tuning_mode == TuningMode::external_voice_intent,
              "a single quiet frame does not immediately leave EVI");
        // 连续 500 ms 静音后退出（回到 SONG_EQ）。
        for (int i = 0; i < 5; ++i) out = flicker.update(frame(-54.0f, 0.0f), 100);
        check(out.tuning_mode == TuningMode::song_eq,
              "500 ms without voice leaves EVI");
    }

    // ---- 对话门槛高于 EQ 门槛：一般噪声不能把对话顶起来 ----
    {
        AdaptController noisy;
        EarbudCommand out;
        // voice 0.6、-45 dBFS：够"有人声"（EQ/场景）但不够对话（0.65）。
        for (int i = 0; i < 12; ++i) {
            out = noisy.update(frame(-45, 0.6f), 150, false, false, false,
                               /*speech_event=*/true);
        }
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

    // ---- 声纹门控纯函数（D28 step3）：只有 self 是否决，other / 无判定都走 D16 ----
    // HEAD 的"双方轮次才授权对话"语义已删除；self 的完整否决路径见上一段。
    {
        const VoiceprintDecision fresh_self = evaluate_voiceprint(true, true, 1000, 3000000);
        check(fresh_self.active && fresh_self.self_voice,
              "fresh self verdict activates the self veto");

        const VoiceprintDecision fresh_other = evaluate_voiceprint(true, false, 1000, 3000000);
        check(fresh_other.active && !fresh_other.self_voice,
              "fresh other verdict is active but does not veto conversation");

        const VoiceprintDecision stale = evaluate_voiceprint(true, true, 4000000, 3000000);
        check(!stale.active && !stale.self_voice,
              "a stale verdict stops affecting decisions (no permanent lock)");

        const VoiceprintDecision never = evaluate_voiceprint(false, false, 0, 3000000);
        check(!never.active && !never.self_voice,
              "no verdict never activates the veto");
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

    // ---- 键盘特殊掩蔽 + 普通噪声掩蔽（2026-10-26，D48）----
    // 真机定位：键盘来时模式停在 SONG_EQ、掩蔽推量 ≈ 0、EQ 只动 ±0.8 dB。这里钉住：
    // ①键盘事件在足迹段（1.0–3.4 kHz）给出清晰但克制的掩蔽抬升；②普通噪声掩蔽按
    // 电平连续缩放；③安静/安全保持不叠掩蔽；④±4 dB 上限不破；⑤键盘停止后掩蔽慢释放
    // 不瞬间抽掉（2026-10-26 现场第一版拉满 +4 dB：人声全变、毛刺感极强）。
    {
        AdaptController typing;
        EarbudCommand typed;
        for (int i = 0; i < 24; ++i) {
            typed = typing.update(frame(-42.0f, 0.0f), 150, /*non_voice_event=*/true,
                                  /*external_self_voice=*/false, /*keyboard_event=*/true);
        }
        check(typed.tuning_mode == TuningMode::song_eq,
              "keyboard stays in song EQ mode (non-voice noise)");
        check(typed.eq_gain_db[10] >= 1.2f,
              "keyboard footprint band gets a clear masking lift");
        check(typed.eq_gain_db[0] > 0.3f && typed.eq_gain_db[0] < typed.eq_gain_db[10],
              "keyboard adds a broadband layer below the footprint emphasis");
        bool capped = true;
        for (float gain : typed.eq_gain_db) {
            if (gain > 4.01f) capped = false;
        }
        check(capped, "keyboard masking respects the ±4 dB per-band device cap");

        AdaptController plain;
        EarbudCommand plain_out;
        for (int i = 0; i < 24; ++i) {
            plain_out = plain.update(frame(-42.0f, 0.0f), 150, /*non_voice_event=*/true);
        }
        check(plain_out.tuning_mode == TuningMode::song_eq,
              "plain noise without the keyboard event stays in song EQ");
        check(typed.eq_gain_db[10] > plain_out.eq_gain_db[10] + 0.8f,
              "the keyboard event adds a distinct emphasis on top of noise masking");

        // 键盘停止后：掩蔽慢释放（1200 ms），不会一拍就抽回噪声掩蔽水平。
        EarbudCommand after_typing;
        for (int i = 0; i < 2; ++i) {
            after_typing = typing.update(frame(-42.0f, 0.0f), 150,
                                         /*non_voice_event=*/true);
        }
        check(after_typing.eq_gain_db[10] > plain_out.eq_gain_db[10] + 0.3f,
              "keyboard mask releases gradually when typing stops");

        AdaptController quiet_noise;
        EarbudCommand q;
        for (int i = 0; i < 24; ++i) q = quiet_noise.update(frame(-54.0f, 0.0f), 150);
        check(q.eq_gain_db[14] < 0.15f,
              "at -54 dBFS the noise mask is off (no masking in a quiet room)");
        check(plain_out.eq_gain_db[14] > q.eq_gain_db[14] + 1.0f,
              "louder steady noise produces a larger presence mask");

        AdaptController typing_quiet;
        EarbudCommand quiet_typed;
        for (int i = 0; i < 12; ++i) {
            quiet_typed = typing_quiet.update(frame(-65.0f, 0.0f), 150,
                                              /*non_voice_event=*/true,
                                              /*external_self_voice=*/false,
                                              /*keyboard_event=*/true);
        }
        check(all_bands_flat(quiet_typed),
              "keyboard masking does not fire in a quiet room (mode stays flat)");

        AdaptController typing_loud;
        EarbudCommand loud_typed;
        for (int i = 0; i < 40; ++i) {
            loud_typed = typing_loud.update(frame(-20.0f, 0.0f), 150,
                                            /*non_voice_event=*/true,
                                            /*external_self_voice=*/false,
                                            /*keyboard_event=*/true);
        }
        float loud_max = 0.0f;
        for (float gain : loud_typed.eq_gain_db) loud_max = std::max(loud_max, gain);
        check(loud_max < 0.05f,
              "keyboard masking respects safety hold (no positive gain when very loud)");
    }

    // ---- 漏音/非人声不得进入对话或背景人声判定（2026-10-26，D49）----
    // 耳边实测：OWS 耳机漏音把 voice_likelihood 顶到 0.82（event=quiet），旧路径把
    // 对话状态机锁进 engaged、EQ 压平 47%。现在 speech_event=false 时必须既不进对话、
    // 也不进背景人声掩蔽，掩蔽在 SONG_EQ 下继续工作。
    {
        AdaptController leaked;
        EarbudCommand out;
        for (int i = 0; i < 24; ++i) {
            out = leaked.update(frame(-45.0f, 0.9f), 150, /*non_voice_event=*/false,
                                /*external_self_voice=*/false, /*keyboard_event=*/false,
                                /*speech_event=*/false);
        }
        check(out.conversation == ConversationState::none,
              "leakage-like voice without a speech event never starts a conversation");
        check(out.tuning_mode == TuningMode::song_eq,
              "leakage-like voice without a speech event stays in song EQ (no voice mask)");
        check(out.eq_gain_db[14] < 2.5f,
              "no D41 voice-band emphasis for leakage (only the level-driven noise mask)");
        check(out.media_percent == 100 && !out.ambient_open,
              "leakage-like voice never ducks media or opens ambient");
    }

    // ---- 高噪声安全保持：禁止正增益，但去糊削减保留（D38）----
    {
        AdaptController safety_controller;
        EarbudCommand loud;
        for (int i = 0; i < 40; ++i) loud = safety_controller.update(frame(-20, 0.0f), 150);
        check(loud.tuning_mode == TuningMode::safety_hold, "very loud non-speech selects safety hold");
        float max_gain = 0.0f;
        float min_gain = 0.0f;
        for (float gain : loud.eq_gain_db) {
            max_gain = std::max(max_gain, gain);
            min_gain = std::min(min_gain, gain);
        }
        check(max_gain < 0.05f, "safety hold forbids positive EQ gain");
        check(min_gain < -0.1f, "safety hold still allows de-mud cuts (negative gains)");
        check(loud.media_percent == 100, "safety hold does not duck without a conversation");
    }

    // ---- 模式路由用平滑电平：单帧瞬态不得把模式顶进 SAFETY_HOLD（D38）----
    {
        AdaptController transient;
        EarbudCommand out;
        for (int i = 0; i < 10; ++i) out = transient.update(frame(-38.0f, 0.0f), 100);
        out = transient.update(frame(-22.0f, 0.0f), 100);  // 键盘/拍桌的一帧尖峰
        check(out.tuning_mode == TuningMode::song_eq,
              "a single 100 ms loud transient does not flip the mode into safety hold");
        check(out.environment == EnvironmentKind::steady_noise,
              "the smoothed level keeps the environment label stable through the transient");
    }

    // ---- 麦克风故障：必须复位对话状态、关环境声、把媒体抬回来 ----
    {
        AdaptController failing;
        for (int i = 0; i < 20; ++i) failing.update(frame(-45, 0.8f), 150,
                                                    false, false, false,
                                                    /*speech_event=*/true);
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
        for (int i = 0; i < 20; ++i) with_demo = demo.update(frame(-28, 0.8f), 150,
                                                              false, false, false,
                                                              /*speech_event=*/true);
        check(with_demo.media_percent <= 61, "near_field_demo also ducks");
        check(with_demo.own_voice.proxy && !with_demo.own_voice.bone,
              "near_field_demo never claims bone evidence");
        check(with_demo.tuning_mode == TuningMode::flat,
              "conversation mode does not lift the song EQ");

        AdaptController off;
        off.set_evidence_policy(EvidencePolicy::disabled);
        EarbudCommand with_disabled;
        for (int i = 0; i < 20; ++i) with_disabled = off.update(frame(-28, 0.8f), 150,
                                                                 false, false, false,
                                                                 /*speech_event=*/true);
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
        for (int i = 0; i < 20; ++i) with_bone = bone.update(frame(-28, 0.8f), 150,
                                                              false, false, false,
                                                              /*speech_event=*/true);
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

        EarbudCommand no_wind;
        no_wind.tuning_mode = TuningMode::song_eq;
        no_wind.conversation = ConversationState::none;
        for (std::size_t band = 0; band < kBandCount; ++band) no_wind.eq_gain_db[band] = 6.0f;
        apply_wind_eq(no_wind, /*wind_active=*/false);
        check(no_wind.eq_gain_db[0] == 6.0f && no_wind.eq_gain_db[4] == 6.0f,
              "wind EQ stays off while the wind debounce is not satisfied (D54)");

        EarbudCommand already_low;
        already_low.tuning_mode = TuningMode::song_eq;
        already_low.eq_gain_db[0] = 1.0f;
        apply_wind_eq(already_low);
        check(already_low.eq_gain_db[0] == 0.25f, "wind EQ reduces, never raises, a low band");

        // 有符号曲线（D38）：低频的负增益（去糊）不能被风策略也乘 1/4——
        // 那等于"风一吹就少削"，与"风噪占满低频、少抬低频"的目标相反。
        EarbudCommand cut_low;
        cut_low.tuning_mode = TuningMode::song_eq;
        cut_low.eq_gain_db[3] = -1.5f;
        cut_low.eq_gain_db[4] = -1.5f;
        apply_wind_eq(cut_low);
        check(cut_low.eq_gain_db[3] == -1.5f && cut_low.eq_gain_db[4] == -1.5f,
              "wind EQ leaves low-band cuts untouched (only lifts are reduced)");
    }

    // ---- 17. A1/D50：Mac 迟到否决回写（dialog no TTL 内强制释放对话态）----
    {
        AdaptController dialogue;
        EarbudCommand engaged;
        for (int i = 0; i < 20; ++i) {
            engaged = dialogue.update(frame(-38, 0.9f), 100, false, false, false,
                                      /*speech_event=*/true);
        }
        check(engaged.media_percent <= 61, "precondition: sustained speech engages the fast path");
        check(engaged.ambient_open, "precondition: engaged opens the environment intent");
        check(engaged.tuning_mode == TuningMode::flat, "precondition: engaged keeps the song EQ flat");

        // 否决后：连续证据继续喂，也必须保持释放——否则电视/播客会把状态机顶回去。
        EarbudCommand after_veto;
        for (int i = 0; i < 30; ++i) {
            after_veto = dialogue.update(frame(-38, 0.9f), 100, false, false, false,
                                         /*speech_event=*/true, /*dialogue_veto=*/true);
        }
        check(!after_veto.ambient_open, "dialog veto closes the ambient intent");
        check(after_veto.conversation != ConversationState::likely,
              "dialog veto releases the conversation state");
        check(after_veto.media_percent >= 99, "media returns to 100% under the veto");
        check(after_veto.tuning_mode == TuningMode::external_voice_intent,
              "background voice falls back to masking (EXTERNAL_VOICE_INTENT) under the veto");
        check(after_veto.external_voice_gain_db > 0.0f,
              "masking still raises the song under the veto");

        // 撤销否决：端侧预判重新生效（0.8 s 连续证据）。
        EarbudCommand restored;
        for (int i = 0; i < 12; ++i) {
            restored = dialogue.update(frame(-38, 0.9f), 100, false, false, false,
                                       /*speech_event=*/true, /*dialogue_veto=*/false);
        }
        check(restored.ambient_open, "clearing the veto restores the local fast path");
        check(restored.media_percent <= 61, "the restored conversation ducks the media again");
    }

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    check(std::string(conversation_state_name(ConversationState::anticipating)) == "ANTICIPATING",
          "the prediction state has a stable protocol name");
    check(EarbudCommand{}.schema_version == 4, "v4 carries the ambient_open intent");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
