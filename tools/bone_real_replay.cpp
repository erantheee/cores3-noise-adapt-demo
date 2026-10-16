// 用**真机采集的**六轴 + 音频包络跑 bone_voice 的判据。
//
// 为什么必须用真机数据：bone_voice 的阈值（vibration_full / corr_min / motion_veto / 重力时间常数）
// 全部是在合成正弦上定的。而这个仓库里已经有一条**否定结论**——ear-context 的 self-voice 探针
// （experiments/ear-context/selfvoice_report.py）用 11 个 IMU 特征做 LOSO 分类，AUC 0.0508、
// 结论"基本不可分，建议停"。我的判据与它不同（用振动包络与音频包络的相关，不是绝对特征），
// 但同样建立在"耳内加速度计能听到自己说话"这个前提上，所以必须先在真数据上验一遍。
//
// 输入 CSV（由 记录/对话感知标定 脚本导出，100 Hz）：
//     t_ms,accel_mag,audio_tick_dbfs,label
// 其中 audio_tick_dbfs 每 10 行给一个值（对应固件的 100 ms 控制拍）。
//
// 编译：g++ -std=c++17 -O2 -I main -o /tmp/bone_real tools/bone_real_replay.cpp main/bone_voice.cpp
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "bone_voice.hpp"

namespace {
struct Row {
    float t_ms = 0.0f;
    float accel = 0.0f;
    bool has_audio = false;
    float audio = -120.0f;
    std::string label;
};

std::vector<Row> load(const char* path) {
    std::vector<Row> rows;
    FILE* f = std::fopen(path, "r");
    if (f == nullptr) { std::printf("打不开 %s\n", path); std::exit(2); }
    char line[512];
    bool first = true;
    while (std::fgets(line, sizeof(line), f) != nullptr) {
        if (first) { first = false; continue; }
        char* p = line;
        const float t = std::strtof(p, &p); if (*p != ',') continue; ++p;
        const float a = std::strtof(p, &p); if (*p != ',') continue; ++p;
        char* q = p;
        while (*q != ',' && *q != '\n' && *q != '\0') ++q;
        Row row;
        row.t_ms = t;
        row.accel = a;
        if (q != p) { row.has_audio = true; row.audio = std::strtof(p, nullptr); }
        if (*q == ',') ++q;
        std::string label(q);
        while (!label.empty() && (label.back() == '\n' || label.back() == '\r')) label.pop_back();
        row.label = label;
        rows.push_back(row);
    }
    std::fclose(f);
    return rows;
}

struct Stats {
    std::string label;
    int ticks = 0;
    std::vector<float> confidence, vibration, correlation;
    float confidence_peak = 0.0f;
    int motion_ticks = 0;
    float confidence_median() const { return median(confidence); }
    float vibration_median() const { return median(vibration); }
    float correlation_median() const { return median(correlation); }
    static float median(std::vector<float> v) {
        if (v.empty()) return 0.0f;
        std::sort(v.begin(), v.end());
        return v[v.size() / 2];
    }
};
}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { std::printf("用法: bone_real <csv>\n"); return 1; }
    const std::vector<Row> rows = load(argv[1]);
    if (rows.empty()) { std::printf("没有数据行\n"); return 2; }
    std::printf("读入 %zu 行（100 Hz，约 %.1f 秒）\n\n", rows.size(), rows.back().t_ms / 1000.0f);

    bone::BoneVoice voice;
    std::vector<Stats> stats;
    std::string current;
    int counter = 0;
    for (const Row& row : rows) {
        voice.push_imu(row.accel, row.t_ms);
        if (!row.has_audio) continue;
        const bone::Output out = voice.push_audio(row.audio, row.t_ms);
        if (row.label != current) {
            current = row.label;
            Stats s; s.label = current;
            stats.push_back(s);
        }
        Stats& s = stats.back();
        ++s.ticks;
        s.confidence.push_back(out.confidence);
        s.vibration.push_back(out.vibration_rms);
        s.correlation.push_back(out.correlation);
        s.confidence_peak = std::max(s.confidence_peak, out.confidence);
        if (out.motion) ++s.motion_ticks;
    }

    std::printf("%-14s%7s%12s%12s%12s%10s\n", "段（真机标签）", "拍数", "振动RMS中位", "相关中位",
                "置信度中位", "置信度峰值");
    for (const Stats& s : stats) {
        std::printf("%-14s%7d%12.4f%12.3f%12.3f%10.3f\n", s.label.c_str(), s.ticks,
                    s.vibration_median(), s.correlation_median(), s.confidence_median(),
                    s.confidence_peak);
    }
    std::printf("\n（振动 RMS 单位 m/s²；当前 Params: vibration_full=%.3f corr_min=%.2f motion_veto=%.2f）\n",
                voice.params().vibration_full, voice.params().corr_min, voice.params().motion_veto);
    return 0;
}
