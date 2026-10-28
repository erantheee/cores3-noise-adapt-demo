#include <algorithm>
#include <iostream>

#include "../main/conversation_awareness.hpp"
#include "w1_timeline_golden.h"

// 预判式对话感知：测触发与时序，不测调音、不测环境声执行。
// 固件控制节拍是 100 ms（main/app_main.cpp 的 kControlPeriodMs），所以这里也按 100 ms 喂。
//
// 目标（2026-10-19 用户口径）：**对话一开始就动作**，不是"我说出口之后才压"。
// 因此核心用例是：没有任何自身语音证据、只有"附近有人说话"的 cue，也必须进入
// engaged 并打开环境声意图。
//
// 2026-10-23 判据（用户拍板"参数用数据选"）：确认/续命保持**连续 800 ms**；
// 新增事件帧中立——keyboard/wind/artifact 帧不打断连续证据、不累计释放计时。
// 数据否决（work/diagnosis/evaluate_gate_params.py，W1 现场噪声 1179 帧回放）：
//   * 滑动窗口累计（1.5s/600ms 等）→ W1 窗内证据最多 11 帧，累计 800 ms 仍有
//     138 个窗口达标 → 误触；
//   * vl 3 帧平滑 → W1 噪声连续段 700→1000 ms → 误触；
//   * 阈值 0.65 不降（用户明确：降阈值+放宽连续两维度叠加风险平方）。
// 事件中立下 W1 最长连续段仍为 7 帧（0 误触），真实对话的键盘打字不再打断确认。

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

conversation::Evidence evidence(float cue, float own_voice = 0.0f,
                                bool event_blocked = false) {
    conversation::Evidence value;
    value.conversation_cue = cue;
    value.own_voice = own_voice;
    value.speech_active = cue > 0.0f || own_voice > 0.0f;
    value.voice_likelihood = cue > own_voice ? cue : own_voice;
    value.level_dbfs = value.speech_active ? -40.0f : -65.0f;
    value.event_blocked = event_blocked;
    return value;
}

/// 喂 n 帧同样的证据，返回最后一帧的输出。
conversation::Output feed(conversation::Awareness& awareness, float cue, int frames,
                          float own_voice = 0.0f, bool event_blocked = false) {
    conversation::Output out;
    for (int i = 0; i < frames; ++i) {
        out = awareness.update(evidence(cue, own_voice, event_blocked), kDt);
    }
    return out;
}

/// 回放 W1 现场噪声时序（1179 帧），返回 (确认次数, 最小媒体百分比, 是否开过环境声)。
struct ReplayResult {
    int confirmations = 0;
    float lowest_media = 100.0f;
    bool any_ambient = false;
};

ReplayResult replay_w1(conversation::Awareness& awareness, bool initial_engaged = false) {
    ReplayResult result;
    bool was_engaged = initial_engaged;
    for (int i = 0; i < kW1TimelineFrames; ++i) {
        const std::int8_t kind = kW1Timeline[i];
        conversation::Evidence ev;
        ev.conversation_cue = kind == 1 ? 0.9f : 0.0f;
        ev.speech_active = kind == 1;
        ev.voice_likelihood = kind == 1 ? 0.9f : 0.0f;
        ev.level_dbfs = kind == 1 ? -40.0f : -65.0f;
        ev.event_blocked = kind == -1;
        const conversation::Output out = awareness.update(ev, kDt);
        if (out.engaged && !was_engaged) ++result.confirmations;
        was_engaged = out.engaged;
        result.lowest_media = std::min(result.lowest_media, out.media_percent);
        result.any_ambient = result.any_ambient || out.ambient_open;
    }
    return result;
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

    // 2. 单帧 blip 不动作：进入 anticipating，但预下压要 500 ms 证据。
    {
        conversation::Awareness awareness;
        auto blip = feed(awareness, 0.9f, 1);
        check(blip.state == conversation::State::anticipating, "one cue frame starts anticipating");
        check(!blip.ambient_open, "anticipation does not open the environment yet");
        check(blip.media_percent > 99.0f,
              "a single cue frame does not duck (preview needs 500 ms of evidence)");
        auto after = feed(awareness, 0.0f, 1);
        check(after.state == conversation::State::idle, "a short blip falls back to idle");
    }

    // 3. 400 ms 连续：还在预测、不预压、不确认。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.9f, 4);
        check(out.state == conversation::State::anticipating,
              "400 ms stays predicting, not confirmed");
        check(!out.anticipating, "preview waits for 500 ms of evidence");
        check(!out.ambient_open, "400 ms does not open the environment");
    }

    // 4. 500 ms 连续：预下压到预览地板，环境声仍关闭。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.9f, 5);
        check(out.state == conversation::State::anticipating, "500 ms stays predicting, not confirmed");
        check(out.anticipating, "the preview flag turns on after 500 ms of evidence");
        check(out.media_percent < 90.0f, "preview ducks once the evidence is sustained");
        check(out.media_percent >= 69.0f, "preview respects its bounded floor");
        check(!out.ambient_open, "preview does not open the environment");
    }

    // 5. 600 ms 连续还在预测；800 ms 确认、压到地板、环境声意图开放。
    {
        conversation::Awareness awareness;
        auto at600 = feed(awareness, 0.9f, 6);
        check(at600.state == conversation::State::anticipating,
              "600 ms stays predicting, not confirmed yet");
        check(!at600.ambient_open, "600 ms does not open the environment");
        auto out = feed(awareness, 0.9f, 2);  // 累计 800 ms
        check(out.state == conversation::State::engaged, "800 ms of evidence reaches engaged");
        check(out.ambient_open, "engaged opens the environment");
        // 下压有一个 150 ms 时间常数，确认后再走 4 拍（400 ms）贴住 60% 地板。
        auto settled = feed(awareness, 0.9f, 4);
        check(settled.media_percent < 65.0f, "engaged ducks the media bed");
        check(settled.media_percent >= 59.0f, "ducking respects the 60% floor");
    }

    // 6. **核心**：没有自身语音证据，只有"对方在说话"也必须动作。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.9f, 10, /*own_voice=*/0.0f);
        check(out.state == conversation::State::engaged,
              "conversation cue alone reaches engaged (prediction does not need my voice)");
        check(out.ambient_open, "conversation cue alone opens the environment");
        check(out.media_percent < 80.0f, "conversation cue alone ducks the media");
    }

    // 7. 对方停下后保持 2.5 s（长于句间换气），期间不抬起媒体、不关闭环境声。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        auto during = feed(awareness, 0.0f, 20);  // 2 s
        check(during.state == conversation::State::engaged, "a breath inside a turn keeps the conversation");
        check(during.media_percent < 65.0f, "media stays down through the breath");
        check(during.ambient_open, "environment stays open through the breath");
    }

    // 8. 对话结束：保持窗过后进入 releasing，环境声立刻关闭，媒体恢复后回到 idle。
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

    // 9. 自身语音证据单独也能触发（保留给将来真正的自身语音检测器）。
    {
        conversation::Awareness awareness;
        auto out = feed(awareness, 0.0f, 10, /*own_voice=*/0.9f);
        check(out.state == conversation::State::engaged, "own voice alone still engages");
        check(out.ambient_open, "own voice alone opens the environment after confirmation");
    }

    // 10. 恢复途中又有人说话：重新走预测→确认，不沿用旧证据。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);
        feed(awareness, 0.0f, 26);  // 过 hold，开始恢复
        auto resumed = feed(awareness, 0.9f, 4);  // 400 ms：不够确认
        check(!resumed.engaged, "new speech after release needs re-confirmation");
        auto confirmed = feed(awareness, 0.9f, 6);  // 再 600 ms，累计 1000 ms
        check(confirmed.engaged, "sustained new speech re-engages after confirmation");
        check(confirmed.ambient_open, "re-engagement re-opens the environment");
    }

    // 11. reset() 必须把状态机、媒体与环境声意图一起带回初始值。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 12);
        awareness.reset();
        auto out = awareness.update(evidence(0.0f), kDt);
        check(out.state == conversation::State::idle, "reset returns to idle");
        check(out.media_percent > 99.0f, "reset restores media");
        check(!out.ambient_open, "reset closes the environment");
    }

    // 12. **W1 现场噪声回放（1179 帧）**：误触率必须不比现状差——零确认、
    //     不压媒体、不开环境声。
    {
        conversation::Awareness awareness;
        const ReplayResult result = replay_w1(awareness);
        check(result.confirmations == 0,
              "W1 noise never confirms a conversation under the event-neutral criterion");
        check(!result.any_ambient, "W1 noise never opens the environment");
        check(result.lowest_media > 60.0f,
              "W1 noise never reaches the confirmed duck floor (no 60% lock)");
    }

    // 13. 键盘事件帧中立：穿插在对话证据里的键盘帧不打断确认累积，也不贡献时长。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 3);                      // 300 ms 证据
        feed(awareness, 0.0f, 3, 0.0f, /*event_blocked=*/true);  // 300 ms 键盘
        auto mid = feed(awareness, 0.9f, 2);           // 再 200 ms，证据累计 500 ms
        check(mid.state == conversation::State::anticipating,
              "keyboard frames neither break nor count toward confirmation");
        auto confirmed = feed(awareness, 0.9f, 3);     // 再 300 ms，累计 800 ms
        check(confirmed.engaged,
              "confirmation arrives once evidence reaches 800 ms despite keyboard");
        // 键盘帧不累计释放计时：对话中 2 s 键盘不应把对话顶掉。
        auto during_keys = feed(awareness, 0.0f, 20, 0.0f, /*event_blocked=*/true);
        check(during_keys.engaged, "keyboard frames do not count toward the release timer");
    }

    // 14. 已确认的对话不能被 W1 噪声续命：真实对话结束后噪声必须让状态机释放。
    {
        conversation::Awareness awareness;
        feed(awareness, 0.9f, 10);  // 先建立一次真实对话
        const ReplayResult tail = replay_w1(awareness, /*initial_engaged=*/true);
        check(tail.confirmations == 0,
              "W1 noise does not re-confirm a finished conversation");
        auto final_out = feed(awareness, 0.0f, 1);
        check(final_out.state != conversation::State::engaged,
              "W1 noise does not keep a finished conversation engaged");
    }

    // 15. 参数是可标定的：门限、确认时长、hold 都要能用差分验证。
    {
        conversation::Awareness raised;
        conversation::Params params;
        params.cue_threshold = 0.95f;
        raised.set_params(params);
        auto below = feed(raised, 0.9f, 20);
        check(!below.engaged && below.state == conversation::State::idle,
              "raised cue threshold rejects the old evidence level");
        auto above = feed(raised, 0.98f, 10);
        check(above.engaged, "the same state machine engages above the threshold");
    }
    {
        conversation::Awareness slower;
        conversation::Params params;
        params.onset_confirm_ms = 1200.0f;
        slower.set_params(params);
        auto early = feed(slower, 0.9f, 8);  // 800 ms
        check(early.state == conversation::State::anticipating,
              "a longer confirmation keeps the conversation predicted, not confirmed");
        check(!early.ambient_open, "unconfirmed prediction keeps the environment closed");
        auto later = feed(slower, 0.9f, 5);  // 再 500 ms，累计 1300 ms
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

    // 16. Mac 迟到否决回写（A1/D50）：`dialog no` TTL 内强制释放对话态。
    {
        conversation::Awareness awareness;
        auto engaged = feed(awareness, 0.9f, 10);
        check(engaged.engaged, "precondition: sustained cue engages");
        check(engaged.ambient_open, "precondition: engaged opens the environment");
        check(engaged.media_percent < 65.0f, "precondition: engaged ducks the media");

        // 否决的第一拍就离开 engaged（进 releasing），环境声意图立刻关闭。
        conversation::Evidence veto = evidence(0.9f);
        veto.dialogue_veto = true;
        auto released = awareness.update(veto, kDt);
        check(released.state == conversation::State::releasing,
              "dialog veto releases an engaged conversation immediately");
        check(!released.ambient_open, "dialog veto closes the environment intent at once");

        // TTL 内**持续**有对话证据（连续电视/播客）也不能重新确认——这是本功能的核心：
        // 现场同一个说话人连续说，取消一次否决后下一拍就会被顶回去，否决就失效了。
        conversation::Output held;
        for (int i = 0; i < 20; ++i) held = awareness.update(veto, kDt);
        check(held.state == conversation::State::idle,
              "a sustained veto keeps the conversation released");
        check(!held.engaged && !held.ambient_open,
              "sustained speech cannot re-engage during the veto");
        check(held.media_percent > 99.0f, "media returns to 100% during the veto");

        // 撤销否决后，端侧预判恢复：重新走 0.5 s 预下压 / 0.8 s 确认。
        auto recovered = feed(awareness, 0.9f, 9);
        check(recovered.engaged, "clearing the veto restores the local fast path");
    }
    {
        // 否决先到、对话证据后到：TTL 内连预测都不该开始。
        conversation::Awareness awareness;
        conversation::Evidence veto = evidence(0.9f);
        veto.dialogue_veto = true;
        conversation::Output out;
        for (int i = 0; i < 20; ++i) out = awareness.update(veto, kDt);
        check(out.state == conversation::State::idle,
              "a veto keeps a new conversation from being predicted");
        check(!out.ambient_open, "a veto keeps the environment closed");
        check(out.media_percent > 99.0f, "a veto leaves the media alone");
    }

    std::cout << "\nconversation_awareness: " << passed << " passed, " << failed << " failed\n";
    return failed == 0 ? 0 : 1;
}
