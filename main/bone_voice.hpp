#pragma once

#include <cstddef>
#include <cstdint>

/// 骨传导自身语音证据。
///
/// 这是 Apple 那条功能的证据来源：AirPods 用**内耳麦克风 + 加速度计**判断"是佩戴者自己在说话"，
/// 而不是"附近有人说话"。本模块只做**特征与判定**，不碰 I2C、不碰任务调度，所以它能在主机上
/// 用合成信号完整测试（见 test/bone_voice_test.cpp）。
///
/// 为什么用"振动包络与音频包络的相关"而不是直接找语音基频：
///   * 100 Hz ODR 的加速度计 Nyquist 只有 50 Hz，看不到 100–1000 Hz 的骨传导语音振动本身；
///   * 但**振动幅度随时间起伏的包络**（音节率 2–8 Hz）落在可观测范围内，而自己的声音会让
///     "振动包络"和"音频包络"同步起伏 —— 别人说话、外放、走动都不会产生这种同步。
///   所以判据是"同步性"，不是"有没有振动"。想直接看振动频带需要把 ODR 提到 400 Hz 以上，
///   那是下一步（见 docs/apple-conversation-awareness.md）。
namespace bone {

struct Params {
    /// 去重力/姿态的时间常数（秒）。等价于一个 ~0.08 Hz 的高通角：把重力直流与转头/摘戴这类
    /// 姿态变化去掉，保留 1 Hz 以上的振动（走动 1–3 Hz、语音引起的振动更高）。
    ///
    /// 注意它**只对交流有意义**：真实加速度计读到的是"重力直流 + 振动交流"。第一版测试用的是
    /// "整拍直流偏置"这种不物理的信号，于是高通的输出被基线漂移主导、RMS 丢掉符号、相关系数
    /// 变成 −1 —— 那是测试信号的错，不是判据的错，但正是它逼着这里把物理假设写清楚。
    float gravity_tau_s = 2.0f;
    /// 相关窗长度（毫秒）。要盖住几个音节，又不至于把"换气"当成不相关。
    float window_ms = 800.0f;
    /// 相关系数 ≥ 这个值算完全相关。
    float corr_full = 0.8f;
    /// 相关系数 ≤ 这个值算没有骨传导证据。
    float corr_min = 0.3f;
    /// 振动 RMS 到这个值（m/s²）算满幅。耳侧/桌面耦合的语音振动量级很小。
    float vibration_full = 0.02f;
    /// 慢窗振动 RMS 超过这个值（m/s²）判为整体运动，直接否决证据（走动时振动大但与语音无关）。
    float motion_veto = 0.35f;
    /// 相关窗至少要有几个控制拍（100 ms 一拍）才算 ready。
    std::size_t min_pairs = 6;
};

struct Output {
    /// 0..1，自身语音（骨传导）置信度。
    float confidence = 0.0f;
    /// 去重力后的振动 RMS（m/s²）。
    float vibration_rms = 0.0f;
    /// 与音频语音带包络的相关系数（-1..1）；窗内方差为 0 时记 0。
    float correlation = 0.0f;
    /// 整体运动否决是否生效。
    bool motion = false;
    /// 相关窗是否已经够长。false 时 confidence 一定是 0。
    bool ready = false;
};

class BoneVoice {
public:
    /// 每个加速度采样调用一次（固件里 100 Hz）。会累进当前 100 ms 桶。
    void push_imu(float accel_magnitude, float t_ms);
    /// 每个控制拍调用一次（固件里 100 ms）。传入这一拍的语音带电平，返回当前输出。
    Output push_audio(float speech_band_dbfs, float t_ms);

    void reset();

    const Params& params() const { return params_; }
    void set_params(const Params& params) { params_ = params; }

private:
    static constexpr std::size_t kMaxWindow = 32;

    Params params_{};
    /// 重力/姿态基线（指数滑动）。
    float gravity_ = 0.0f;
    bool gravity_valid_ = false;

    /// 自上一个控制拍以来的振动平方和与样本数。桶由 push_audio 关闭，
    /// 所以 ODR 与控制节拍的比例怎么变都不用改这里。
    double bucket_sq_ = 0.0;
    std::size_t bucket_count_ = 0;
    /// 上一个 IMU 采样的时间戳，用来算真实的采样间隔（-1 表示还没有）。
    float last_t_ms_ = -1.0f;

    /// 相关窗：振动 RMS 与音频包络（线性幅度）成对存放。
    float vib_window_[kMaxWindow]{};
    float audio_window_[kMaxWindow]{};
    std::size_t window_count_ = 0;
    std::size_t window_next_ = 0;

    /// 慢窗振动 RMS（运动判据）。
    float motion_rms_ = 0.0f;
};

}  // namespace bone
