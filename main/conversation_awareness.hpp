#pragma once

#include <cstdint>

/// Apple AirPods「对话感知」(Conversation Awareness) 的端侧等价实现。
///
/// Apple 公开的行为只有三句话（出处见 docs/apple-conversation-awareness.md）：
///   1. 佩戴者**自己**开口时，媒体音量下降；
///   2. 说话期间保持低位（接近暂停）；
///   3. 对话结束后恢复。
/// 触发条件是"自己开口"，不是"附近有人说话"——播客、电视、路人说话都不触发。旧实现用的
/// 轮次启发式（语音→停顿→再语音）检的是"这段音频像不像对话"，方向就是错的。
///
/// 本模块只负责**触发与时序**：不碰降噪/通透/ANC，也不改调音。下压深度只作用在媒体音量上。
///
/// 证据与状态机分开：模块不计算任何特征。这样把今天的近场占位证据换成声纹门控或 IMU
/// 骨传导证据时，状态机与全部时序参数都不用改（与 main/voiceprint_gate.hpp 同一种切法）。
namespace conversation {

/// 每一帧由调用方填好交进来。
struct Evidence {
    bool speech_active = false;
    float voice_likelihood = 0.0f;
    /// 0..1，佩戴者自己开口的置信度。这是唯一的触发输入。
    float own_voice = 0.0f;
    float level_dbfs = -120.0f;
};

/// 触发与时序参数集中在这里，便于按实测一次性标定。
///
/// **这些默认值是待标定值，不是 Apple 的实测值。** Apple 没有公开任何数字；我们自己
/// 在 macOS + AirPods Pro (固件 9A348) 上的两轮实测也没能测到下压（记录见文档）。
struct Params {
    /// "自己开口"的置信度门限。
    float own_voice_threshold = 0.5f;
    /// 需要持续多久才算真的开始说话。低于它的短促声音（咳嗽、桌椅、键盘）不触发。
    float onset_confirm_ms = 200.0f;
    /// 首帧已像“自己开口”、但还在确认时，先做一次有界的预下压。这样用户在第一个
    /// 100 ms 控制拍就能感到功能开始工作；第二拍确认失败时会立即恢复，不会把一次
    /// 短促声音当成完整对话。
    float preview_floor_percent = 70.0f;
    /// 预下压时间常数。它只在 confirming 状态使用，不改变正式下压与恢复的时序。
    float preview_duck_ms = 60.0f;
    /// 下压到的音量（%）。与 main/controller.hpp 的 kDemoMediaFloorPercent 同源，改要一起改。
    float duck_floor_percent = 60.0f;
    /// 下压的时间常数（下降沿）。
    float duck_ms = 150.0f;
    /// 语音停止后保持低谷的时长。Apple 的描述是"说话期间保持低"，所以它必须长于句间
    /// 换气（否则媒体在每句话之间抬回来），短于"对话已经结束"的判断。
    float hold_ms = 2500.0f;
    /// 恢复的时间常数（上升沿）。
    float restore_ms = 400.0f;
};

enum class State : std::uint8_t {
    idle,
    /// 已经听到自己开口，但还没持续够 onset_confirm_ms；媒体只做有界预下压。
    confirming,
    /// 对话进行中：媒体应当处于下压状态。
    engaged,
    /// 对话结束、媒体正在抬回来。
    releasing,
};

struct Output {
    State state = State::idle;
    bool engaged = false;
    float media_percent = 100.0f;
    float own_voice = 0.0f;
    /// 本次对话已持续多久。真机上用它验证 hold/restore 时序，也进遥测。
    std::uint32_t engaged_ms = 0;
    /// 距上一次"自己开口"过了多久。
    std::uint32_t silence_ms = 0;
};

/// 今天的"自身语音"证据近似：**近场电平优势 x 语音判据**。
///
/// 设备上还没有声纹/骨传导检测器（voiceprint_gate 只有接口、主工程没接模型，IMU 也没进主工程），
/// 所以先用这个可标定的占位。它不是 Apple 的判据：Apple 用内耳麦克风 + 加速度计的骨传导证据，
/// 因此这份代理**已知会把"附近有人大声说话"误当成佩戴者开口**。
///
/// 接入真正的检测器时，只换掉调用点传进来的 Evidence::own_voice，状态机与全部时序参数不动。
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
    float silence_ms_ = 0.0f;
    std::uint32_t engaged_ms_ = 0;
};

const char* state_name(State state);

}  // namespace conversation
