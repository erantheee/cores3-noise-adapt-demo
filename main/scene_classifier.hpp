#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>

#include "context_window.hpp"
#include "scene_model.h"

/// 端侧「环境场景」判断：安静 / 街道 / 高速 / 市集 / 人声 / 其他。
///
/// 它与 9 类听音情境模型消费同一个 2 秒 × 92 维窗，但只回答环境场景。为什么另做一条：
/// 9 类模型在真机上长期输出 `other`（街道、外放音乐、真实说话都中），不能拿它的 argmax
/// 当场景。这里用同一份固件特征，先去掉每个窗的整体电平（把 24 段均值减去 level_mean），
/// 再跑一个 92→3 的线性头。这样街道/高速的差别来自谱形，而不是手机外放的音量钮。
///
/// 训练数据：三段长录音（公园旁街道 / 夜间城市高速 / 菜市场），特征由
/// `experiments/listening-model/scripts/features.py` 按固件口径提取。验证按 30 秒块切分，
/// 不是窗口随机切分。**这是文件域验证，不是 S3 麦克风域验收**；面板必须同时显示这一点。
namespace scene {

/// 与 92 维窗向量一致的索引（context_window.hpp）。
constexpr std::size_t kLevelMean = 72;
constexpr std::size_t kVoiceMean = 76;
constexpr std::size_t kFlatness = 80;
constexpr std::size_t kVocalRatio = 82;

/// 安静门槛：文件域街道约 -31 dBFS、高速约 -23、菜市场约 -33；房间本底通常低于 -50。
/// 该值只用于“别把房间本底硬判成街道”，不是产品声压级定义。
constexpr float kQuietDbfs = -48.0f;
/// 低于这个置信度不硬报，显示“其他”。
constexpr float kMinConfidence = 0.55f;
/// top1 与 top2 至少差这么多，否则视为混合/不确定。
constexpr float kMinMargin = 0.12f;
/// 人声规则：市集的窗均 voice 约 0.66、flatness 约 0.086；真人近讲通常更高、更尖。
constexpr float kSpeechVoice = 0.86f;
constexpr float kSpeechFlatness = 0.12f;
constexpr float kSpeechVocalRatio = 0.62f;
/// 分布外门槛：教室/房间本底的谱平坦度远高于三类训练素材（街道 p95 0.094、高速 0.064、
/// 市集 0.111；2026-10-30 实机本底 0.199）。线性头在分布外会给出 0.999 的 street，
/// 所以先用平坦度拒识，别让“房间很安静”被画成“街道”。
constexpr float kMaxFlatness = 0.12f;
constexpr float kOodFlatness = 0.10f;
constexpr float kOodVoice = 0.50f;
constexpr float kMaxRmsZ = 2.0f;
/// 场景切换保持：3 拍 = 300 ms。2 秒窗本身已经有惯性，再加 300 ms 避免边界抖动。
constexpr int kHoldFrames = 3;

inline const char* name(SceneKind kind) {
    switch (kind) {
        case SceneKind::quiet: return "quiet";
        case SceneKind::street: return "street";
        case SceneKind::highway: return "highway";
        case SceneKind::market: return "market";
        case SceneKind::speech: return "speech";
        case SceneKind::other: return "other";
        case SceneKind::unknown: return "unknown";
    }
    return "unknown";
}

/// 一个窗的判断。`valid=false` = 窗还没满，不是“判断为 unknown”。
struct Result {
    bool valid = false;
    SceneKind kind = SceneKind::unknown;
    float score = 0.0f;
};

class Classifier {
public:
    void reset() {
        stable_ = SceneKind::unknown;
        candidate_ = SceneKind::unknown;
        candidate_frames_ = 0;
    }

    /// `vector` 是 context::ContextWindow::build_vector() 的输出，长度 92。
    Result update(const float* vector, bool voice_likely = false) {
        Result out;
        if (vector == nullptr) return out;
        out.valid = true;

        const float level = vector[kLevelMean];
        SceneKind kind = SceneKind::unknown;
        float score = 0.0f;

        if (level < kQuietDbfs) {
            kind = SceneKind::quiet;
            score = 1.0f;
        } else if (voice_likely) {
            kind = SceneKind::speech;
            score = vector[kVoiceMean];
        } else if (is_speech(vector)) {
            kind = SceneKind::speech;
            score = vector[kVoiceMean];
        } else if (vector[kFlatness] > kMaxFlatness ||
                   (vector[kFlatness] > kOodFlatness && vector[kVoiceMean] < kOodVoice)) {
            kind = SceneKind::other;
            score = 0.0f;
        } else {
            float shape[scene_model::kFeatureCount];
            for (std::size_t i = 0; i < scene_model::kFeatureCount; ++i) {
                shape[i] = vector[i];
            }
            for (std::size_t i = 0; i < 24; ++i) shape[i] -= level;

            double z2 = 0.0;
            for (std::size_t i = 0; i < scene_model::kFeatureCount; ++i) {
                const double z = (shape[i] - scene_model::kMu[i]) / scene_model::kSigma[i];
                z2 += z * z;
            }
            const float rms_z = static_cast<float>(
                std::sqrt(z2 / static_cast<double>(scene_model::kFeatureCount)));
            if (rms_z > kMaxRmsZ) {
                kind = SceneKind::other;
                score = 0.0f;
            } else {

            float logits[scene_model::kClassCount];
            scene_model::classify(shape, logits);
            std::size_t top1 = 0;
            std::size_t top2 = 1;
            if (logits[top2] > logits[top1]) {
                const std::size_t swap = top1;
                top1 = top2;
                top2 = swap;
            }
            for (std::size_t c = 2; c < scene_model::kClassCount; ++c) {
                if (logits[c] > logits[top1]) {
                    top2 = top1;
                    top1 = c;
                } else if (logits[c] > logits[top2]) {
                    top2 = c;
                }
            }
            const float max = logits[top1];
            float sum = 0.0f;
            float probs[scene_model::kClassCount];
            for (std::size_t c = 0; c < scene_model::kClassCount; ++c) {
                probs[c] = std::exp(logits[c] - max);
                sum += probs[c];
            }
            for (std::size_t c = 0; c < scene_model::kClassCount; ++c) probs[c] /= sum;
            const float top1_prob = probs[top1];
            const float top2_prob = probs[top2];
            score = top1_prob;
            if (top1_prob < kMinConfidence || top1_prob - top2_prob < kMinMargin) {
                kind = SceneKind::other;
            } else if (top1 == 0) {
                kind = SceneKind::street;
            } else if (top1 == 1) {
                kind = SceneKind::highway;
            } else {
                kind = SceneKind::market;
            }
            }
        }

        // Hold: don't let a single boundary window flip the label. The first valid window
        // is reported immediately; afterwards a change needs kHoldFrames consecutive votes.
        if (stable_ == SceneKind::unknown) {
            stable_ = kind;
            candidate_ = kind;
            candidate_frames_ = 0;
        } else if (kind == stable_) {
            candidate_ = kind;
            candidate_frames_ = 0;
        } else if (kind == candidate_) {
            if (++candidate_frames_ >= kHoldFrames) {
                stable_ = kind;
                candidate_frames_ = 0;
            }
        } else {
            candidate_ = kind;
            candidate_frames_ = 1;
        }
        out.kind = stable_;
        out.score = score;
        return out;
    }

private:
    static bool is_speech(const float* v) {
        return v[kVoiceMean] > kSpeechVoice && v[kFlatness] > kSpeechFlatness &&
               v[kVocalRatio] > kSpeechVocalRatio;
    }

    SceneKind stable_ = SceneKind::unknown;
    SceneKind candidate_ = SceneKind::unknown;
    int candidate_frames_ = 0;
};

}  // namespace scene
