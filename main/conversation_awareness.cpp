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
    // A1/D50：Mac 的迟到否决回写（`dialog no` 的 TTL 内）。语义只是"这段不是对话"：
    //   1. 已经建立/正在建立的对话态立即释放（进 releasing，媒体按 restore 常数平滑
    //      回 100%），环境声意图随 engaged 一起关掉；
    //   2. TTL 内新的对话证据一律不认——现场场景是连续电视/播客，同一段语音会在
    //      下一拍把状态机顶回 engaged，否决就失效了。
    // 噪声/EQ 路径不受影响：本拍仍然照常算 voice/环境，背景人声掩蔽（D41/D48）继续。
    const bool veto = evidence.dialogue_veto;
    if (veto) {
        if (state_ == State::engaged || state_ == State::anticipating) {
            state_ = State::releasing;
            gap_ms_ = params_.hold_ms;
            silence_ms_ = 0.0f;
        }
        evidence_streak_ = 0;
    }
    const bool cue = !veto && evidence.conversation_cue >= params_.cue_threshold;
    const bool own = !veto && evidence.own_voice >= params_.own_voice_threshold;
    const bool evidence_present = cue || own;
    // 2026-10-23：事件帧（键盘/风/撞击）中立——不打断连续证据、不累计确认/释放
    // 计时。数据依据见 work/diagnosis/evaluate_gate_params.py（W1 1179 帧回放：
    // 窗口累计与 vl 平滑都被否决；事件中立是唯一零误触的改善）。
    if (evidence_present) {
        evidence_streak_ += 1;
    } else if (!evidence.event_blocked) {
        evidence_streak_ = 0;
    }

    switch (state_) {
        case State::idle:
            if (evidence_present) {
                state_ = State::anticipating;
                confirm_ms_ = dt;
                gap_ms_ = 0.0f;
                silence_ms_ = 0.0f;
                engaged_ms_ = 0;
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
            } else if (evidence.event_blocked) {
                // 键盘/风/撞击：不打断确认累积，也不贡献时长。
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
            // 续命条件：连续 800 ms 证据（2026-10-21 起；事件帧中立不打断）。
            if (evidence_present && evidence_streak_ >= 8) {
                gap_ms_ = 0.0f;
                silence_ms_ = 0.0f;
            } else if (evidence.event_blocked) {
                // 键盘不累计释放计时：打字期间的对话保持期不被打断。
            } else {
                gap_ms_ += dt;
                silence_ms_ += dt;
                if (gap_ms_ >= params_.hold_ms) state_ = State::releasing;
            }
            break;

        case State::releasing:
            if (evidence_present) {
                // 恢复期里又出现证据：重新走一遍预测→确认，和一次全新对话一致，
                // 避免断续噪声在 2.5 s hold 周期上反复"放回来→压下去"。
                state_ = State::anticipating;
                confirm_ms_ = dt;
                gap_ms_ = 0.0f;
                silence_ms_ = 0.0f;
                if (confirm_ms_ >= params_.onset_confirm_ms) {
                    state_ = State::engaged;
                    gap_ms_ = 0.0f;
                }
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

    Output out;
    out.state = state_;
    out.engaged = state_ == State::engaged;
    // 预下压也要求证据持续到 preview_confirm_ms。现场噪声的连续段最长 640 ms，
    // p90 只有 215 ms；如果第一帧就压，媒体会在噪声里持续 100%↔70% 抽动。
    out.anticipating = state_ == State::anticipating &&
                       confirm_ms_ >= params_.preview_confirm_ms;

    float target = 100.0f;
    float tau = params_.restore_ms;
    if (state_ == State::engaged) {
        target = params_.duck_floor_percent;
        tau = params_.duck_ms;
    } else if (out.anticipating) {
        target = std::clamp(params_.preview_floor_percent,
                            params_.duck_floor_percent, 100.0f);
        tau = params_.preview_duck_ms;
    }
    media_percent_ = approach(media_percent_, target, dt, tau);

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
