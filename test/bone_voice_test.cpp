#include <cmath>
#include <iostream>
#include <vector>

#include "../main/bone_voice.hpp"

// 骨传导自身语音证据：只测特征与判定，不碰 I2C 与任务调度。
// 合成信号故意做成"包络同步/不同步"两组，因为判据本身就是**同步性**：
// 自己的声音让加速度计振动和音频包络一起起伏，别人说话/外放/走动都不会。

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const char* name) {
    if (condition) { ++passed; std::cout << "PASS " << name << '\n'; }
    else { ++failed; std::cout << "FAIL " << name << '\n'; }
}

constexpr float kGravity = 9.81f;
constexpr int kImuPerTick = 10;   // 100 Hz IMU / 10 Hz 控制拍
constexpr float kTickMs = 100.0f;

/// 一个说话/停顿的包络模板（8 拍一循环）：前三拍说话，一拍停，一拍说话，三拍停。
const bool kSpeaking[8] = {true, true, true, false, true, false, false, false};

/// 20 Hz 振动：真实加速度计读到的是"重力直流 + 振动交流"，不是直流偏置。
constexpr float kVibrationHz = 20.0f;

float accel_sample(float amplitude, float t_ms) {
    return kGravity + amplitude * std::sin(2.0f * 3.14159265f * kVibrationHz * t_ms / 1000.0f);
}

/// 按"每拍一个振动幅度 + 每拍一个音频电平"跑完整条序列。
bone::Output run(bone::BoneVoice& voice, const std::vector<float>& vibration,
                 const std::vector<float>& audio_dbfs, bone::Output* last = nullptr,
                 float* peak_confidence = nullptr) {
    bone::Output out;
    float t = 0.0f;
    for (std::size_t tick = 0; tick < vibration.size(); ++tick) {
        for (int i = 0; i < kImuPerTick; ++i) {
            voice.push_imu(accel_sample(vibration[tick], t), t);
            t += kTickMs / kImuPerTick;
        }
        out = voice.push_audio(audio_dbfs[tick], t);
        if (last != nullptr) *last = out;
        // 证据是**逐拍**给出的：说话有起伏，停顿那一拍本来就该低。要判定"这段是自己在说"，
        // 看峰值而不是最后一拍。
        if (peak_confidence != nullptr) *peak_confidence = std::max(*peak_confidence, out.confidence);
    }
    return out;
}

std::vector<float> speaking_pattern(float loud, float quiet, int ticks) {
    std::vector<float> out;
    for (int i = 0; i < ticks; ++i) out.push_back(kSpeaking[i % 8] ? loud : quiet);
    return out;
}

/// 走动/敲击这类**振荡**振动：真实运动是来回振，不是恒定偏置（恒定偏置会被基线吸掉，
/// 那是姿态变化，不是运动）。
std::vector<float> oscillation_pattern(float amplitude, int ticks) {
    std::vector<float> out(ticks, amplitude);
    return out;
}
}  // namespace

int main() {
    // 1. 完全安静：振动恒定、音频恒定 → 两侧方差都是 0，不构成证据。
    {
        bone::BoneVoice voice;
        std::vector<float> vib(20, 0.0005f);
        std::vector<float> audio(20, -70.0f);
        bone::Output out;
        run(voice, vib, audio, &out);
        check(out.ready, "silence still fills the window");
        check(out.confidence == 0.0f, "silence produces no bone evidence");
        check(std::fabs(out.correlation) < 1e-6f, "silence has no correlation to report");
    }

    // 2. 自己说话：振动包络与音频包络同步 → 证据高。
    {
        bone::BoneVoice voice;
        auto vib = speaking_pattern(0.03f, 0.0005f, 24);
        auto audio = speaking_pattern(-25.0f, -70.0f, 24);
        bone::Output out;
        float peak = 0.0f;
        run(voice, vib, audio, &out, &peak);
        check(out.ready, "own speech fills the window");
        check(out.correlation > 0.8f, "own speech correlates");
        check(peak > 0.5f, "own speech yields bone evidence");
        check(!out.motion, "normal speech level is not motion");
    }

    // 3. 别人说话 / 外放：音频在起伏，但加速度计不动 → 没有证据。
    {
        bone::BoneVoice voice;
        std::vector<float> vib(24, 0.0005f);
        auto audio = speaking_pattern(-25.0f, -70.0f, 24);
        bone::Output out;
        run(voice, vib, audio, &out);
        check(out.confidence < 0.05f, "external speech yields no bone evidence");
    }

    // 4. 走动：振荡振动（RMS 远超 motion_veto）而且与音频不同步 → 运动否决 + 不相关。
    {
        bone::BoneVoice voice;
        bone::Output out;
        float t = 0.0f;
        auto audio = speaking_pattern(-25.0f, -70.0f, 24);
        for (std::size_t tick = 0; tick < audio.size(); ++tick) {
            for (int i = 0; i < kImuPerTick; ++i) {
                // 2 Hz、幅度 0.6 的走动振动：与音频包络无关，且远超运动门限。
                voice.push_imu(kGravity + 0.6f * std::sin(2.0f * 3.14159265f * 2.0f * t / 1000.0f), t);
                t += kTickMs / kImuPerTick;
            }
            out = voice.push_audio(audio[tick], t);
        }
        check(out.motion, "walking trips the motion veto");
        check(out.confidence == 0.0f, "walking produces no bone evidence");
    }

    // 5. 走路时也在说话：运动否决优先，宁可不触发（误触发比漏触发更烦人）。
    {
        bone::BoneVoice voice;
        bone::Output out;
        float t = 0.0f;
        auto audio = speaking_pattern(-25.0f, -70.0f, 24);
        for (std::size_t tick = 0; tick < audio.size(); ++tick) {
            for (int i = 0; i < kImuPerTick; ++i) {
                // 边走边说：振动很大，而且因为幅度接近，和音频包络也不相关。
                voice.push_imu(accel_sample(kSpeaking[tick % 8] ? 0.62f : 0.55f, t), t);
                t += kTickMs / kImuPerTick;
            }
            out = voice.push_audio(audio[tick], t);
        }
        check(out.motion, "walking while talking still counts as motion");
        check(out.confidence == 0.0f, "motion veto wins over correlation");
    }

    // 6. 窗还没满时不下结论：ready=false 且 confidence 必须是 0。
    {
        bone::BoneVoice voice;
        auto vib = speaking_pattern(0.03f, 0.0005f, 3);
        auto audio = speaking_pattern(-25.0f, -70.0f, 3);
        bone::Output out;
        run(voice, vib, audio, &out);
        check(!out.ready, "three ticks is not a window");
        check(out.confidence == 0.0f, "no evidence before the window is full");
    }

    // 7. 参数入口（真机标定就靠它）：同一段输入，换阈值就给不同结论。
    {
        bone::BoneVoice strict;
        auto vib = speaking_pattern(0.03f, 0.0005f, 24);
        auto audio = speaking_pattern(-25.0f, -70.0f, 24);
        const float strict_confidence = run(strict, vib, audio).confidence;

        bone::BoneVoice lenient;
        bone::Params params;
        params.corr_min = 0.0f;
        params.corr_full = 0.5f;
        params.vibration_full = 0.01f;
        lenient.set_params(params);
        const float lenient_confidence = run(lenient, vib, audio).confidence;
        check(lenient_confidence > strict_confidence,
              "relaxing the thresholds raises the same evidence");
        check(strict_confidence > 0.0f && lenient_confidence <= 1.0f,
              "both stay inside 0..1");
    }

    // 8. reset()：必须把重力基线、窗、运动判据一起清掉，否则断流之后会拿旧证据。
    {
        bone::BoneVoice voice;
        auto vib = speaking_pattern(0.03f, 0.0005f, 24);
        auto audio = speaking_pattern(-25.0f, -70.0f, 24);
        run(voice, vib, audio);
        voice.reset();
        auto fresh = speaking_pattern(0.03f, 0.0005f, 2);
        auto fresh_audio = speaking_pattern(-25.0f, -70.0f, 2);
        bone::Output out;
        run(voice, fresh, fresh_audio, &out);
        check(!out.ready, "reset drops the window");
        check(out.confidence == 0.0f, "reset drops the evidence");
    }

    std::cout << "\nbone_voice: " << passed << " passed, " << failed << " failed\n";
    return failed == 0 ? 0 : 1;
}
