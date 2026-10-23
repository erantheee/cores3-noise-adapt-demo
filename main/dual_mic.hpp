#pragma once

/// 双麦克风遥测（2026-10-23，D47 取数阶段）。
///
/// CoreS3 板载双麦（ES7210 两路输入，见 managed_components/.../es7210.c 的默认
/// mic_select = MIC1 | MIC2），但旧固件 I2S 只开 MONO 单通道，第二路麦的采样被丢弃。
/// 这一轮只做**取数**：I2S 开 STEREO，把两路样本的帧级统计随 earbud_command 上报，
/// 用于真机看"近场说话 / 远场噪声 / 安静"三态下两路麦的区分度，再定近场判据阈值。
///
/// 物理口径（先讲清楚，不猜）：
///   * 两路是**同一次 I2S 采样**的 L/R 槽，样本对齐、无时差；
///   * 两颗麦间距约 1–2 cm：近场声源（嘴对设备）到达两麦的相位/电平差大，
///     **相干性低、电平差大**；远场（房间混响、电视、远处人声）两麦近似相同信号，
///     相干性高、电平差小。这是"近/远"判据的物理基础，但阈值必须用真机数据标定；
///   * 全部统计只做遥测：**不改任何决策**，主判据仍走第 1 路（L），行为与单麦完全一致。
///
/// 这个头文件不依赖 ESP-IDF，主机可编译并跑单测（test/dual_mic_test.cpp）。
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace dual_mic {

/// 帧内累加器：逐块喂 L/R 交织的 int16 样本，帧完成后 `finish()` 出统计。
/// 内存极小（6 个 double），放在处理任务栈上即可。
struct Accumulator {
    double sum_l = 0.0;     // Σ L
    double sum_r = 0.0;     // Σ R
    double sum_l2 = 0.0;    // Σ L²
    double sum_r2 = 0.0;    // Σ R²
    double sum_lr = 0.0;    // Σ L·R
    std::size_t frames = 0; // 已累计的采样帧数（L/R 各一）

    /// `interleaved` 为 L,R,L,R,… 交织数据，`frame_count` 为 L/R 对数。
    void push(const int16_t* interleaved, std::size_t frame_count) {
        if (interleaved == nullptr) return;
        for (std::size_t i = 0; i < frame_count; ++i) {
            const double l = static_cast<double>(interleaved[2 * i]) / 32768.0;
            const double r = static_cast<double>(interleaved[2 * i + 1]) / 32768.0;
            sum_l += l;
            sum_r += r;
            sum_l2 += l * l;
            sum_r2 += r * r;
            sum_lr += l * r;
        }
        frames += frame_count;
    }

    void reset() {
        sum_l = 0.0;
        sum_r = 0.0;
        sum_l2 = 0.0;
        sum_r2 = 0.0;
        sum_lr = 0.0;
        frames = 0;
    }
};

/// 一帧的双麦统计。`valid=false` = 这一帧没有双麦样本（旧路径/输入中断）。
struct Stats {
    float left_dbfs = -120.0f;   // L 路宽带 RMS 电平
    float right_dbfs = -120.0f;  // R 路宽带 RMS 电平
    float diff_db = 0.0f;        // L − R（正 = L 更响）
    /// 帧内两路的 Pearson 相关系数，∈ [-1, 1]。近场声源期望低（甚至接近 0 或负），
    /// 远场/混响期望高（接近 1）。分母不可用时为 0（由 valid 区分"没样本"）。
    float coherence = 0.0f;
    bool valid = false;
};

/// 汇总累加器。纯函数，便于主机单测与离线复算。
inline Stats finish(const Accumulator& acc) {
    Stats out;
    if (acc.frames == 0) return out;
    const double n = static_cast<double>(acc.frames);
    const double mean_l = acc.sum_l / n;
    const double mean_r = acc.sum_r / n;
    const double var_l = acc.sum_l2 / n - mean_l * mean_l;
    const double var_r = acc.sum_r2 / n - mean_r * mean_r;
    const double cov = acc.sum_lr / n - mean_l * mean_r;

    out.left_dbfs = 20.0 * std::log10(std::sqrt(std::max(acc.sum_l2 / n, 1.0e-12)));
    out.right_dbfs = 20.0 * std::log10(std::sqrt(std::max(acc.sum_r2 / n, 1.0e-12)));
    out.diff_db = out.left_dbfs - out.right_dbfs;
    out.valid = true;
    if (var_l > 0.0 && var_r > 0.0) {
        const double r = cov / std::sqrt(var_l * var_r);
        out.coherence = static_cast<float>(std::clamp(r, -1.0, 1.0));
    } else {
        // 一路恒定（纯静音/削波平台）：协方差没有意义，报 0。
        out.coherence = 0.0f;
    }
    return out;
}

}  // namespace dual_mic
