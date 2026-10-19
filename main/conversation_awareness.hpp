#pragma once

#include <cstdint>

/// 对话感知：**预判式**让位，而不是事后反应。
///
/// 总目标（2026-10-19 用户口径）：不是"检测到我在说话才压低"，而是"对话一开始（通常是
/// 对方先开口）就把媒体压下去、并把环境声放进来"，等我接话时音乐已经让位、耳朵已经打开。
///
/// 所以触发输入是 `conversation_cue`（对话开始的证据），`own_voice` 只是可选的确认证据。
/// 旧版把 `own_voice` 当唯一触发输入，只能在"我已经说出口"之后动作，方向与目标相反；
/// 上一轮加的预下压也只是把"事后"提前了 100 ms，仍然不是预判。
///
/// 本模块只负责**触发与时序**，不计算任何特征。环境声开放只是一个布尔意图：
/// demo 里由 Mac 切 AirPods 聆听模式执行，最终产品由耳机自己的通透 DSP 执行。
namespace conversation {

/// 每一帧由调用方填好交进来。
struct Evidence {
    bool speech_active = false;
    float voice_likelihood = 0.0f;
    /// 0..1，**对话开始**的证据：附近有人在说话（近场）。提前量只可能来自这里。
    float conversation_cue = 0.0f;
    /// 0..1，佩戴者自己开口的置信度。它只是**可选确认**；今天设备上没有真正可靠的
    /// 自身语音检测器（骨传导已被本仓库真机数据否定），所以不能把它当唯一触发。
    float own_voice = 0.0f;
    float level_dbfs = -120.0f;
};

/// 触发与时序参数集中在这里，便于按实测一次性标定。
///
/// **这些默认值是待标定值，不是 Apple 的实测值。** Apple 没有公开任何数字；本项目
/// 要做的行为也与 Apple 不同（Apple 是"我开始说"，这里是"预测对话开始"）。
struct Params {
    /// 对话开始证据的门限。过线即进入 anticipating，并立刻预下压媒体。
    float cue_threshold = 0.5f;
    /// 自身语音证据门限（可选确认）。
    float own_voice_threshold = 0.5f;
    /// 证据要持续多久，才把"预测"升级为"确认"。短促声音（咳嗽、桌椅、键盘）到不了
    /// 这里就退回 idle——**环境声模式不会为一声咳嗽切来切去**。
    float onset_confirm_ms = 200.0f;
    /// 预测阶段的预下压：第一拍就有可感知的响应，但不把媒体一次打到底。
    float preview_floor_percent = 70.0f;
    /// 预下压时间常数（只在 anticipating 使用）。
    float preview_duck_ms = 60.0f;
    /// 确认之后的正式下压地板（%）。与 main/controller.hpp 的 kDemoMediaFloorPercent 同源。
    float duck_floor_percent = 60.0f;
    /// 正式下压的时间常数（下降沿）。
    float duck_ms = 150.0f;
    /// 对话中最后一个人声结束后保持低谷的时长。必须长于句间换气，否则媒体会在
    /// 每句话之间抬回来。
    float hold_ms = 2500.0f;
    /// 恢复的时间常数（上升沿）。
    float restore_ms = 400.0f;
};

enum class State : std::uint8_t {
    idle,
    /// 预测到对话开始：媒体已预下压，环境声还没打开。
    anticipating,
    /// 对话成立：媒体压到地板，环境声开放意图为 true。
    engaged,
    /// 人声结束、媒体正在抬回来，环境声意图已关闭。
    releasing,
};

struct Output {
    State state = State::idle;
    bool engaged = false;
    bool anticipating = false;
    /// 环境声开放意图：`State::engaged` 时为 true。执行端负责把它变成"通透模式"之类
    /// 的真实动作；没有执行回执时，它只是意图，不是"耳朵已经打开"。
    bool ambient_open = false;
    float media_percent = 100.0f;
    float own_voice = 0.0f;
    /// 本次对话已持续多久（从预测到对话开始算起）。
    std::uint32_t engaged_ms = 0;
    /// 距上一次对话证据过了多久。
    std::uint32_t silence_ms = 0;
};

/// 近场自身语音代理：**已知只等于"有人在我附近说话"**，分不出是谁。
/// 保留它只是为了有可标定的占位证据与回放对比；产品路径默认不把它当自身语音。
float near_field_own_voice(float level_dbfs, float voice_likelihood);

class Awareness {
public:
    Output update(const Evidence& evidence, float dt_ms);
    void reset();

    const Params& params() const { return params_; }
    /// 标定入口：整套替换参数即可，不需要动状态机、也不需要重新编译调用方。
    void set_params(const Params& params) { params_ = params; }

private:
    Params params_{};
    State state_ = State::idle;
    float media_percent_ = 100.0f;
    float confirm_ms_ = 0.0f;
    float gap_ms_ = 0.0f;
    float silence_ms_ = 0.0f;
    std::uint32_t engaged_ms_ = 0;
};

const char* state_name(State state);

}  // namespace conversation
