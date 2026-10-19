#include "conversation_awareness.hpp"

#include <algorithm>
#include <cmath>

namespace conversation {
namespace {

/// 一阶趋近。tau 由调用点按状态给定，不按 target 大小自动挑——旧实现踩过
/// "下压（target 变小）走 release 常数"的坑。
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
    const bool cue = evidence.conversation_cue >= params_.cue_threshold;
    const bool own = evidence.own_voice >= params_.own_voice_threshold;
    const bool evidence_present = cue || own;
    evidence_streak_ = evidence_present ? evidence_streak_ + 1 : 0;

    switch (state_) {
        case State::idle:
            if (evidence_present) {
                state_ = State::anticipating;
                confirm_ms_ = 0.0f;
                gap_ms_ = 0.0f;
                silence_ms_ = 0.0f;
                engaged_ms_ = 0;
                confirm_ms_ = dt;
                if (confirm_ms_ >= params_.onset_confirm_ms) {
                    state_ = State::engaged;
                    gap_ms_ = 0.0f;
                }
            }
            break;

        case State::anticipating:
            engaged_ms_ += static_cast<std::uint32_t>(dt);
            if (evidence_present) {
                confirm_ms_ += dt;
                silence_ms_ = 0.0f;
                if (confirm_ms_ >= params_.onset_confirm_ms) {
                    // 预测升级为确认：从这里开始环境声意图变 true。
                    state_ = State::engaged;
                    gap_ms_ = 0.0f;
                }
            } else {
                // 短促声不升级，也不累积：一声咳嗽退回 idle，环境声没有动过。
                state_ = State::idle;
                confirm_ms_ = 0.0f;
                silence_ms_ = 0.0f;
                engaged_ms_ = 0;
            }
            break;

        case State::engaged:
            engaged_ms_ += static_cast<std::uint32_t>(dt);
            // 连续两帧才算"还在说"；单帧噪声尖峰不让关闭倒计时无限续命。
            if (evidence_present && evidence_streak_ >= 2) {
                gap_ms_ = 0.0f;
                silence_ms_ = 0.0f;
            } else {
                gap_ms_ += dt;
                silence_ms_ += dt;
                if (gap_ms_ >= params_.hold_ms) state_ = State::releasing;
            }
            break;

        case State::releasing:
            if (evidence_present) {
                // 又说起来了：直接回到下压、环境声重新打开。恢复尾巴不属于对话。
                state_ = State::engaged;
                gap_ms_ = 0.0f;
                silence_ms_ = 0.0f;
            } else {
                silence_ms_ += dt;
                if (media_percent_ >= 99.0f) {
                    state_ = State::idle;
                    silence_ms_ = 0.0f;
                    engaged_ms_ = 0;
                }
            }
            break;
    }

    float target = 100.0f;
    float tau = params_.restore_ms;
    if (state_ == State::engaged) {
        target = params_.duck_floor_percent;
        tau = params_.duck_ms;
    } else if (state_ == State::anticipating) {
        target = std::clamp(params_.preview_floor_percent,
                            params_.duck_floor_percent, 100.0f);
        tau = params_.preview_duck_ms;
    }
    media_percent_ = approach(media_percent_, target, dt, tau);

    Output out;
    out.state = state_;
    out.engaged = state_ == State::engaged;
    out.anticipating = state_ == State::anticipating;
    // 环境声只在对话确认后打开：预测阶段先只压媒体，避免被一声咳嗽切到通透模式。
    out.ambient_open = state_ == State::engaged;
    out.media_percent = media_percent_;
    out.own_voice = evidence.own_voice;
    out.engaged_ms = engaged_ms_;
    out.silence_ms = static_cast<std::uint32_t>(silence_ms_);
    return out;
}

void Awareness::reset() {
    evidence_streak_ = 0;
    state_ = State::idle;
    media_percent_ = 100.0f;
    confirm_ms_ = 0.0f;
    gap_ms_ = 0.0f;
    silence_ms_ = 0.0f;
    engaged_ms_ = 0;
}

const char* state_name(State state) {
    switch (state) {
        case State::idle: return "IDLE";
        case State::anticipating: return "ANTICIPATING";
        case State::engaged: return "ENGAGED";
        case State::releasing: return "RELEASING";
    }
    return "UNKNOWN";
}

}  // namespace conversation
