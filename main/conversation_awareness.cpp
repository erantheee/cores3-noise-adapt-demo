#include "conversation_awareness.hpp"

#include <algorithm>
#include <cmath>

namespace conversation {
namespace {

/// 一阶趋近。tau 取下降/上升各自的常数，所以调用点必须自己选对 tau——
/// 这里不用 controller.cpp 里那个按 target 大小自动挑 attack/release 的写法，
/// 那种写法在下压（target 变小）时会走 release 常数，是这个项目已经踩过一次的坑。
float approach(float current, float target, float dt_ms, float tau_ms) {
    const float alpha = 1.0f - std::exp(-std::max(dt_ms, 0.0f) / std::max(tau_ms, 1.0f));
    return current + (target - current) * alpha;
}

}  // namespace

float near_field_own_voice(float level_dbfs, float voice_likelihood) {
    constexpr float kNearFieldLowDb = -45.0f;
    constexpr float kNearFieldHighDb = -25.0f;
    const float level = std::clamp((level_dbfs - kNearFieldLowDb) /
                                       (kNearFieldHighDb - kNearFieldLowDb),
                                   0.0f, 1.0f);
    return std::clamp(voice_likelihood, 0.0f, 1.0f) * level;
}

Output Awareness::update(const Evidence& evidence, float dt_ms) {
    const float dt = std::max(dt_ms, 0.0f);
    const bool own_voice = evidence.own_voice >= params_.own_voice_threshold;

    switch (state_) {
        case State::idle:
            if (own_voice) {
                state_ = State::confirming;
                confirm_ms_ = dt;
            }
            break;

        case State::confirming:
            if (own_voice) {
                confirm_ms_ += dt;
                if (confirm_ms_ >= params_.onset_confirm_ms) {
                    state_ = State::engaged;
                    engaged_ms_ = 0;
                    silence_ms_ = 0.0f;
                }
            } else {
                // 短促声音不触发，而且**不累积**：否则"每隔一会儿响一下"会被攒成一次对话。
                state_ = State::idle;
                confirm_ms_ = 0.0f;
            }
            break;

        case State::engaged:
            engaged_ms_ += static_cast<std::uint32_t>(dt);
            if (own_voice) {
                silence_ms_ = 0.0f;
            } else {
                silence_ms_ += dt;
                if (silence_ms_ >= params_.hold_ms) state_ = State::releasing;
            }
            break;

        case State::releasing:
            // 注意：这里**不**再累加 engaged_ms。它报的是"这次对话持续了多久"，
            // 恢复尾巴不属于对话；在真机上用它验证 hold/restore 时序，多算就废了。
            if (own_voice) {
                // 又说起来了：直接回到下压。对话中间的换气不该让媒体先抬回来再压下去。
                state_ = State::engaged;
                silence_ms_ = 0.0f;
            } else if (media_percent_ >= 99.0f) {
                // 99% 就是 -0.09 dB，人耳和媒体链路都分辨不出；卡在 99.5% 只会让状态机
                // 在 releasing 里多待两个时间常数（指数趋近要 5τ 才到 99.5%）。
                state_ = State::idle;
                engaged_ms_ = 0;
                silence_ms_ = 0.0f;
            }
            break;
    }

    const float target = state_ == State::engaged ? params_.duck_floor_percent : 100.0f;
    const float tau = target < media_percent_ ? params_.duck_ms : params_.restore_ms;
    media_percent_ = approach(media_percent_, target, dt, tau);

    Output out;
    out.state = state_;
    out.engaged = state_ == State::engaged;
    out.media_percent = media_percent_;
    out.own_voice = evidence.own_voice;
    out.engaged_ms = engaged_ms_;
    out.silence_ms = static_cast<std::uint32_t>(silence_ms_);
    return out;
}

void Awareness::reset() {
    state_ = State::idle;
    media_percent_ = 100.0f;
    confirm_ms_ = 0.0f;
    silence_ms_ = 0.0f;
    engaged_ms_ = 0;
}

const char* state_name(State state) {
    switch (state) {
        case State::idle: return "IDLE";
        case State::confirming: return "CONFIRMING";
        case State::engaged: return "ENGAGED";
        case State::releasing: return "RELEASING";
    }
    return "UNKNOWN";
}

}  // namespace conversation
