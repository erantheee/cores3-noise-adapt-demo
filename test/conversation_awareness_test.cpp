#include <iostream>

#include "../main/conversation_awareness.hpp"

// Apple 对话感知的端侧状态机：只测触发与时序，不测调音、不测降噪。
// 固件控制节拍是 100 ms（main/app_main.cpp 的 kControlPeriodMs），所以这里也按 100 ms 喂。

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const char* name) {
    if (condition) {
        ++passed;
        std::cout << "PASS " << name << '\n';
    } else {
        ++failed;
        std::cout << "FAIL " << name << '\n';
    }
}

constexpr float kDt = 100.0f;

conversation::Evidence evidence(float own_voice, bool speech_active = true) {
    conversation::Evidence value;
    value.own_voice = own_voice;
    value.voice_likelihood = own_voice;
    value.speech_active = speech_active;
    value.level_dbfs = own_voice > 0.0f ? -30.0f : -65.0f;
    return value;
}

/// 喂 n 帧同样的证据，返回最后一帧的输出。
conversation::Output feed(conversation::Awareness& awareness, float own_voice, int frames,
                          bool speech_active = true) {
    conversation::Output out;
    for (int i = 0; i < frames; ++i) out = awareness.update(evidence(own_voice, speech_active), kDt);
    return out;
}
}  // namespace

int main() {
    // 1. 安静：什么都不做。这条同时保证"没有证据就不会误下压"。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.0f, 30, false);
        check(out.state == conversation::State::idle, "quiet stays idle");
        check(out.media_percent > 99.0f, "quiet leaves media alone");
        check(!out.engaged, "quiet is not engaged");
    }

    // 2. 短促声音不触发完整对话：首帧允许有界预下压，但不会进入 engaged，也不累积。
    {
        conversation::Awareness awareness;
        auto blip = feed(awareness, 0.9f, 1);
        check(blip.state == conversation::State::confirming, "one frame only confirms");
        check(blip.media_percent >= 69.0f, "one frame preview stays above its bounded floor");
        check(blip.media_percent < 90.0f, "one frame gives immediate preview feedback");
        auto after = feed(awareness, 0.0f, 1, false);
        check(after.state == conversation::State::idle, "a blip falls back to idle");
        // 隔一会儿再来一下，仍然不该攒成对话。
        for (int i = 0; i < 6; ++i) {
            feed(awareness, 0.9f, 1);
            feed(awareness, 0.0f, 3, false);
        }
        auto repeated = feed(awareness, 0.0f, 1, false);
        check(repeated.state == conversation::State::idle, "repeated blips never accumulate");
        check(repeated.media_percent > 90.0f, "repeated blips recover instead of accumulating");
    }

    // 3. 佩戴者自己开口：确认 200 ms 之后开始下压，并压到地板。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.9f, 12);  // 1.2 s
        check(out.engaged, "own voice engages");
        check(out.state == conversation::State::engaged, "own voice reaches engaged");
        check(out.media_percent < 65.0f, "own voice ducks the media bed");
        check(out.media_percent >= 59.0f, "ducking respects the 60% floor");
        check(out.engaged_ms >= 900, "engaged_ms counts the conversation");
    }

    // 4. 触发延迟：第一个 100 ms 拍必须已有响应，接近 60% 地板不超过 300 ms。
    {
        conversation::Awareness awareness;
        auto first = awareness.update(evidence(0.9f), kDt);
        check(first.media_percent < 90.0f, "preview starts on the first control tick");
        int frames_to_duck = 0;
        for (int i = 2; i <= 20; ++i) {
            auto out = awareness.update(evidence(0.9f), kDt);
            if (out.media_percent < 65.0f) { frames_to_duck = i; break; }
        }
        check(frames_to_duck > 0 && frames_to_duck * 100 <= 300,
              "full duck approaches the 60 percent floor within 300 ms");
    }

    // 5. 换气停顿不抬起媒体：停 2 s（< hold 2.5 s）后仍是 engaged。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        auto during = feed(awareness, 0.0f, 20, false);  // 2 s
        check(during.engaged, "a breath inside a turn keeps the conversation");
        check(during.media_percent < 65.0f, "media stays down through the breath");
    }

    // 6. 对话结束后恢复：停够 hold + 恢复常数之后回到 100%，并且回到 idle。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        auto holding = feed(awareness, 0.0f, 24, false);  // 2.4 s，还没到 hold
        check(holding.engaged, "conversation holds just under the hold window");
        feed(awareness, 0.0f, 2, false);                  // 过 hold，进入 releasing
        auto restored = feed(awareness, 0.0f, 30, false); // 3 s 恢复
        check(restored.state == conversation::State::idle, "conversation returns to idle");
        check(restored.media_percent > 99.0f, "media comes back after the conversation");
    }

    // 7. 附近有人说话（不是自己开口）不触发。这正是与旧轮次启发式的差别：
    //    播客、电视、路人说话在频谱上和对话没有区别，Apple 的判据是"自己开口"。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.0f, 60);  // speech_active = true, own_voice = 0
        check(out.state == conversation::State::idle, "other people talking does not trigger");
        check(out.media_percent > 99.0f, "other people talking does not duck");
    }

    // 8. 恢复中途自己又开口：直接回到下压，不重新确认、也不抬起来又压下去。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        feed(awareness, 0.0f, 26, false);                 // 过 hold，开始恢复
        auto resumed = feed(awareness, 0.9f, 1);
        check(resumed.engaged, "speaking again re-engages immediately");
    }

    // 9. reset() 必须把状态机和媒体一起带回初始值，麦克风故障后不能留着下压。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 12);
        awareness.reset();
        auto out = awareness.update(evidence(0.0f, false), kDt);
        check(out.state == conversation::State::idle, "reset returns to idle");
        check(out.media_percent > 99.0f, "reset restores media");
    }

    // 10. 参数是可标定的：真机标定就靠这个入口，所以两条关键参数都要用**差分**验证，
    //     而不是只验证"改了参数之后还能跑"。
    {
        conversation::Awareness raised;
        conversation::Params params;
        params.own_voice_threshold = 0.95f;
        raised.set_params(params);
        auto below = feed(raised, 0.9f, 20);
        check(!below.engaged, "raised threshold rejects the old evidence level");
        auto above = feed(raised, 0.98f, 6);
        check(above.engaged, "the same state machine engages above the threshold");
    }
    {
        // hold_ms = 500 ms 时，0.8 s 静默就该进入恢复；默认 2500 ms 时同样 0.8 s 必须还在对话里。
        conversation::Awareness short_hold;
        conversation::Params short_params;
        short_params.hold_ms = 500.0f;
        short_hold.set_params(short_params);
        feed(short_hold, 0.9f, 10);
        auto short_out = feed(short_hold, 0.0f, 8, false);
        check(short_out.state != conversation::State::engaged,
              "shorter hold leaves the conversation sooner");

        conversation::Awareness default_hold;
        feed(default_hold, 0.9f, 10);
        auto default_out = feed(default_hold, 0.0f, 8, false);
        check(default_out.state == conversation::State::engaged,
              "the same 0.8 s of silence is still inside the default hold");
    }
    {
        // 恢复尾巴跑完之后必须真的回到 idle，且媒体回到 100%。
        conversation::Awareness awareness;
        conversation::Params params;
        params.hold_ms = 500.0f;
        params.restore_ms = 400.0f;
        awareness.set_params(params);
        feed(awareness, 0.9f, 10);
        auto released = feed(awareness, 0.0f, 40, false);  // 4 s
        check(released.state == conversation::State::idle, "release tail ends in idle");
        check(released.media_percent > 99.0f, "release tail restores the media");
    }

    std::cout << "\nconversation_awareness: " << passed << " passed, " << failed << " failed\n";
    return failed == 0 ? 0 : 1;
}
