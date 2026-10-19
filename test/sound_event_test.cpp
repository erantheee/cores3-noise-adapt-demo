// Tests for main/sound_event.hpp: 1 s event window, floor tracker, rule, state machine.
//
// Why this suite exists: the event detector is a hand port of
// `experiments/sound-events/features.py` + `evaluate.py`. A wrong index or a stale threshold
// does not raise anything -- the device just keeps reporting the wrong event. The golden
// vectors in `sound_event_golden.h` are produced by the training-side implementation on real
// CoreS3 frames, so the two implementations are compared feature by feature and class by class.
//
// Regenerate the golden after any change to the recipe or to the Python side:
//     .venv/bin/python experiments/sound-events/export_golden.py \
//         --out test/sound_event_golden.h \
//         记录/键盘风声/20261019-141545/live.csv \
//         记录/键盘风声/20261019-142748-speech/live.csv
#include <cmath>
#include <cstring>
#include <iostream>
#include <string>

#include "../main/sound_event.hpp"
#include "sound_event_golden.h"

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const std::string& name) {
    if (condition) {
        ++passed;
        std::cout << "PASS " << name << '\n';
    } else {
        ++failed;
        std::cout << "FAIL " << name << '\n';
    }
}

/// float32 output against a float64 reference: tolerance is set by float32 rounding plus the
/// 4-decimal rounding of the golden dump, not by any slack in the comparison.
bool close_enough(float actual, float expected) {
    const double tolerance = 1.0e-3 + 1.0e-3 * std::fabs(static_cast<double>(expected));
    return std::fabs(static_cast<double>(actual) - static_cast<double>(expected)) <= tolerance;
}

std::array<sound_event::WindowFrame, sound_event_golden::kWindowFrames> make_frames(
    const sound_event_golden::Sample& sample) {
    std::array<sound_event::WindowFrame, sound_event_golden::kWindowFrames> frames{};
    for (std::size_t i = 0; i < frames.size(); ++i) {
        sound_event::WindowFrame frame;
        frame.band_dbfs = sample.bands[i];
        frame.broadband_dbfs = sample.broadband[i];
        sound_event::frame_descriptors(frame.band_dbfs, &frame);
        frames[i] = frame;
    }
    return frames;
}

void test_features_match_golden() {
    int mismatched = 0;
    for (const auto& sample : sound_event_golden::kSamples) {
        const auto frames = make_frames(sample);
        const sound_event::Features features = sound_event::compute_window_features(
            sample.base, sample.base_level, frames);
        const float actual[sound_event::kFeatureCount] = {
            features.d_lo, features.d_mid, features.d_hi, features.mid_lo, features.hi_mid,
            features.cent, features.flat, features.vr, features.std_lo, features.range_lo,
            features.std_mid, features.level_rel, features.vocal_peak, features.slope,
            features.low_share,
        };
        for (std::size_t i = 0; i < sound_event::kFeatureCount; ++i) {
            if (!close_enough(actual[i], sample.expected[i])) {
                ++mismatched;
                std::cout << "  feature mismatch " << sample.label << " dim " << i
                          << " actual " << actual[i] << " expected " << sample.expected[i]
                          << '\n';
            }
        }
    }
    check(mismatched == 0,
          "15 维窗特征与真机金标准逐值一致（" +
              std::to_string(std::size(sound_event_golden::kSamples)) + " 个窗）");
}

void test_classify_matches_golden() {
    int mismatched = 0;
    for (const auto& sample : sound_event_golden::kSamples) {
        const auto frames = make_frames(sample);
        const sound_event::Features features = sound_event::compute_window_features(
            sample.base, sample.base_level, frames);
        const sound_event::Class predicted = sound_event::raw_classify(features);
        if (std::strcmp(sound_event::class_name(predicted), sample.expected_class) != 0) {
            ++mismatched;
            std::cout << "  classify mismatch " << sample.label << " actual "
                      << sound_event::class_name(predicted) << " expected "
                      << sample.expected_class << '\n';
        }
    }
    check(mismatched == 0, "默认阈值下的判类与训练侧一致");
}

/// 10 帧同电平的频段，用于地板跟踪器测试。
std::array<float, sound_event::kBandCount> flat_bands(float db) {
    std::array<float, sound_event::kBandCount> band{};
    band.fill(db);
    return band;
}

void test_floor_tracker() {
    sound_event::FloorTracker tracker;
    for (int i = 0; i < 10; ++i) {
        tracker.push(flat_bands(-70.0f), -50.0f, true);
    }
    check(tracker.ready(), "地板跟踪：10 帧后完成初始化");
    check(std::fabs(tracker.base()[0] - (-70.0f)) < 0.01f, "地板跟踪：初始值等于安静电平");

    // 响 40 dB 的帧不应该把地板抬走（向上速率 0.002，10 帧 ≈ 0.8 dB）。
    for (int i = 0; i < 10; ++i) {
        tracker.push(flat_bands(-30.0f), -10.0f, true);
    }
    check(tracker.base()[0] < -69.0f, "地板跟踪：持续响 40 dB 时地板抬升 < 1 dB");
    check(std::fabs(tracker.level() - (-50.0f)) < 1.0f, "地板跟踪：宽带电平同样不上抬");

    // 安静下来要跟得上（向下速率 0.25）。
    for (int i = 0; i < 10; ++i) {
        tracker.push(flat_bands(-85.0f), -65.0f, true);
    }
    check(tracker.base()[0] < -83.0f, "地板跟踪：安静下来 1 秒内跟到 -83 以下");
    check(tracker.level() < -63.0f, "地板跟踪：宽带电平跟下来");
}

void test_state_machine() {
    sound_event::StateMachine machine;
    machine.reset();
    check(machine.update(sound_event::Class::keyboard, 0.0f) == sound_event::Class::keyboard,
          "状态机：键盘进入 keyboard");
    check(machine.update(sound_event::Class::quiet, 0.5f) == sound_event::Class::keyboard,
          "状态机：键盘保持 0.8 s 内不退");
    check(machine.update(sound_event::Class::quiet, 0.9f) == sound_event::Class::quiet,
          "状态机：键盘保持到期退回 quiet");

    machine.update(sound_event::Class::keyboard, 1.0f);
    check(machine.update(sound_event::Class::wind, 1.5f) == sound_event::Class::keyboard,
          "状态机：键盘保持期内风不覆盖键盘");
    check(machine.update(sound_event::Class::wind, 2.0f) == sound_event::Class::wind,
          "状态机：键盘保持到期后风生效");
    check(machine.update(sound_event::Class::quiet, 3.5f) == sound_event::Class::wind,
          "状态机：风保持 2 s 内不退");
    check(machine.update(sound_event::Class::quiet, 4.1f) == sound_event::Class::quiet,
          "状态机：风保持到期退回 quiet");

    machine.update(sound_event::Class::speech, 5.0f);
    check(machine.update(sound_event::Class::quiet, 5.5f) == sound_event::Class::speech,
          "状态机：说话保持 0.8 s 内不退");
    check(machine.update(sound_event::Class::quiet, 5.9f) == sound_event::Class::quiet,
          "状态机：说话保持到期退回 quiet");

    machine.update(sound_event::Class::artifact, 6.0f);
    check(machine.update(sound_event::Class::quiet, 7.0f) == sound_event::Class::quiet,
          "状态机：有效帧恢复后立即退出 artifact（artifact 不是声音类别，不能保持）");
}

void test_artifact_clears_window() {
    sound_event::Detector detector;
    detector.reset();
    // 前 3 秒标定出安静地板。
    for (int i = 0; i < 30; ++i) {
        detector.push(flat_bands(-70.0f), -50.0f, true, 0.1f * static_cast<float>(i));
    }
    sound_event::Output out = detector.push(flat_bands(-70.0f), -50.0f, true, 3.0f);
    check(out.valid, "撞击测试：窗在 10 帧后有效");

    out = detector.push(flat_bands(-10.0f), 0.0f, false, 3.1f);
    check(!out.valid && out.cls == sound_event::Class::artifact,
          "撞击测试：无效帧立即报 artifact 且不产生有效窗");

    // 撞击后 9 帧还不够，第 10 帧才重新有效。
    for (int i = 0; i < 9; ++i) {
        out = detector.push(flat_bands(-70.0f), -50.0f, true, 3.2f + 0.1f * static_cast<float>(i));
    }
    check(!out.valid, "撞击测试：清窗后 9 帧仍无效");
    out = detector.push(flat_bands(-70.0f), -50.0f, true, 4.2f);
    check(out.valid, "撞击测试：清窗后第 10 帧恢复有效");
    // 关键回归：valid=true 的那一帧绝不能再报 artifact（协议规定 artifact 只在 valid=false）。
    check(out.cls != sound_event::Class::artifact,
          "撞击测试：恢复有效的帧不再报 artifact（上位机协议一致）");
    check(out.cls == sound_event::Class::quiet, "撞击测试：恢复后按本轮输入判为 quiet");
}

void test_invalid_frame_does_not_move_floor() {
    sound_event::Detector detector;
    detector.reset();
    for (int i = 0; i < 30; ++i) {
        detector.push(flat_bands(-70.0f), -50.0f, true, 0.1f * static_cast<float>(i));
    }
    for (int i = 0; i < 10; ++i) {
        detector.push(flat_bands(-20.0f), 0.0f, false, 3.0f + 0.1f * static_cast<float>(i));
    }
    // 再喂安静帧：如果无效帧把地板抬走了，前几帧的相对电平会偏负；这里只验证窗仍能有效。
    sound_event::Output out;
    for (int i = 0; i < 20; ++i) {
        out = detector.push(flat_bands(-70.0f), -50.0f, true, 4.0f + 0.1f * static_cast<float>(i));
    }
    check(out.valid, "无效帧不污染地板：恢复后窗仍有效");
}

}  // namespace

int main() {
    test_features_match_golden();
    test_classify_matches_golden();
    test_floor_tracker();
    test_state_machine();
    test_artifact_clears_window();
    test_invalid_frame_does_not_move_floor();
    std::cout << "\n" << passed << " passed, " << failed << " failed\n";
    return failed == 0 ? 0 : 1;
}
