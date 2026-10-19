#include <iostream>

#include "../main/conversation_awareness.hpp"

// 预判式对话感知：测触发与时序，不测调音、不测环境声执行。
// 固件控制节拍是 100 ms（main/app_main.cpp 的 kControlPeriodMs），所以这里也按 100 ms 喂。
//
// 目标（2026-10-19 用户口径）：**对话一开始就动作**，不是"我说出口之后才压"。
// 因此核心用例是 4：没有任何自身语音证据、只有"附近有人说话"的 cue，也必须进入
// engaged 并打开环境声意图。

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

conversation::Evidence evidence(float cue, float own_voice = 0.0f) {
    conversation::Evidence value;
    value.conversation_cue = cue;
    value.own_voice = own_voice;
    value.speech_active = cue > 0.0f || own_voice > 0.0f;
    value.voice_likelihood = cue > own_voice ? cue : own_voice;
    value.level_dbfs = value.speech_active ? -40.0f : -65.0f;
    return value;
}

/// 喂 n 帧同样的证据，返回最后一帧的输出。
conversation::Output feed(conversation::Awareness& awareness, float cue, int frames,
                          float own_voice = 0.0f) {
    conversation::Output out;
    for (int i = 0; i < frames; ++i) out = awareness.update(evidence(cue, own_voice), kDt);
    return out;
}
}  // namespace

int main() {
    // 1. 安静：什么都不做。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.0f, 30);
        check(out.state == conversation::State::idle, "quiet stays idle");
        check(out.media_percent > 99.0f, "quiet leaves media alone");
        check(!out.ambient_open, "quiet keeps the environment closed");
    }

    // 2. 预测：第一拍就有预下压，但环境声不打开；一声咳嗽（单帧）不会留在对话态。
    {
        conversation::Awareness awareness;
        auto blip = feed(awareness, 0.9f, 1);
        check(blip.state == conversation::State::anticipating, "one cue frame starts anticipating");
        check(!blip.ambient_open, "anticipation does not open the environment yet");
        check(blip.media_percent < 90.0f, "anticipation ducks on the first control tick");
        check(blip.media_percent >= 69.0f, "anticipation preview respects its bounded floor");
        auto after = feed(awareness, 0.0f, 1);
        check(after.state == conversation::State::idle, "a short blip falls back to idle");
    }

    // 3. 持续对话开始：200 ms 后确认、压到地板、环境声意图变为开放。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.9f, 12);
        check(out.state == conversation::State::engaged, "sustained cue reaches engaged");
        check(out.ambient_open, "engaged opens the environment");
        check(out.media_percent < 65.0f, "engaged ducks the media bed");
        check(out.media_percent >= 59.0f, "ducking respects the 60% floor");
        check(out.engaged_ms >= 1000, "engaged_ms counts the conversation");
    }

    // 4. **核心**：没有自身语音证据，只有"对方在说话"也必须动作。
    //    旧实现（own_voice 是唯一触发输入）在这里会全程 idle，正是"反应太慢"的根因。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.9f, 5, /*own_voice=*/0.0f);
        check(out.state == conversation::State::engaged,
              "conversation cue alone reaches engaged (prediction does not need my voice)");
        check(out.ambient_open, "conversation cue alone opens the environment");
        check(out.media_percent < 80.0f, "conversation cue alone ducks the media");
    }

    // 5. 对方停下后保持 2.5 s（长于句间换气），期间不抬起媒体、不关闭环境声。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        auto during = feed(awareness, 0.0f, 20);  // 2 s
        check(during.state == conversation::State::engaged, "a breath inside a turn keeps the conversation");
        check(during.media_percent < 65.0f, "media stays down through the breath");
        check(during.ambient_open, "environment stays open through the breath");
    }

    // 6. 对话结束：保持窗过后进入 releasing，环境声立刻关闭，媒体恢复后回到 idle。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        auto holding = feed(awareness, 0.0f, 24);  // 2.4 s，还没到 hold
        check(holding.engaged, "conversation holds just under the hold window");
        feed(awareness, 0.0f, 2);                  // 过 hold，进入 releasing
        auto releasing = feed(awareness, 0.0f, 1);
        check(releasing.state == conversation::State::releasing, "conversation ends in releasing");
        check(!releasing.ambient_open, "releasing closes the environment immediately");
        auto restored = feed(awareness, 0.0f, 30);  // 3 s 恢复
        check(restored.state == conversation::State::idle, "conversation returns to idle");
        check(restored.media_percent > 99.0f, "media comes back after the conversation");
        check(!restored.ambient_open, "environment stays closed after recovery");
    }

    // 7. 自身语音证据单独也能触发（保留给将来真正的自身语音检测器）。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.0f, 5, /*own_voice=*/0.9f);
        check(out.state == conversation::State::engaged, "own voice alone still engages");
        check(out.ambient_open, "own voice alone opens the environment after confirmation");
    }

    // 8. 恢复途中又有人说话：立刻回到 engaged，环境声重新打开，不重新确认。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        feed(awareness, 0.0f, 26);  // 过 hold，开始恢复
        auto resumed = feed(awareness, 0.9f, 1);
        check(resumed.state == conversation::State::engaged, "new speech re-engages immediately");
        check(resumed.ambient_open, "re-engagement re-opens the environment");
    }

    // 9. reset() 必须把状态机、媒体与环境声意图一起带回初始值。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 12);
        awareness.reset();
        auto out = awareness.update(evidence(0.0f), kDt);
        check(out.state == conversation::State::idle, "reset returns to idle");
        check(out.media_percent > 99.0f, "reset restores media");
        check(!out.ambient_open, "reset closes the environment");
    }

    // 10. 参数是可标定的：门限、确认时长、hold 都要能用差分验证。
    {
        conversation::Awareness raised;
        conversation::Params params;
        params.cue_threshold = 0.95f;
        raised.set_params(params);
        auto below = feed(raised, 0.9f, 20);
        check(!below.engaged && below.state == conversation::State::idle,
              "raised cue threshold rejects the old evidence level");
        auto above = feed(raised, 0.98f, 6);
        check(above.engaged, "the same state machine engages above the threshold");
    }
    {
        conversation::Awareness slow;
        conversation::Params params;
        params.onset_confirm_ms = 800.0f;
        slow.set_params(params);
        auto early = feed(slow, 0.9f, 5);  // 500 ms
        check(early.state == conversation::State::anticipating,
              "a longer confirmation keeps the conversation predicted, not confirmed");
        check(!early.ambient_open, "unconfirmed prediction keeps the environment closed");
        auto later = feed(slow, 0.9f, 4);  // 再 400 ms
        check(later.engaged, "confirmation arrives once the longer window elapses");
    }
    {
        conversation::Awareness short_hold;
        conversation::Params short_params;
        short_params.hold_ms = 500.0f;
        short_hold.set_params(short_params);
        feed(short_hold, 0.9f, 10);
        auto short_out = feed(short_hold, 0.0f, 8);  // 0.8 s
        check(short_out.state != conversation::State::engaged,
              "shorter hold leaves the conversation sooner");
    }

    std::cout << "\nconversation_awareness: " << passed << " passed, " << failed << " failed\n";
    return failed == 0 ? 0 : 1;
}
