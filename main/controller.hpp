#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "conversation_awareness.hpp"

constexpr std::size_t kBandCount = 24;
/// Largest analysis frame the window table will hold. The audio task uses 2048 samples.
constexpr std::size_t kMaxAnalysisFrame = 4096;
constexpr float kMaxSpeechGainDb = 6.5f;

/// Demo exaggeration. The numbers above are the *product* policy: bounded, smooth, and chosen so
/// a real listener is not startled. This instrument exists to answer a different question -- "is
/// this闭-loop even happening, and how much is it doing?" -- and at 5.4 dB with a flat EQ the
/// answer looked like "nothing". A demo has to be visible.
///
/// The ceilings are inherited from the user's standing rule for this project (no test may exceed
/// +10 dB of gain) and from the measured OWS result in the sibling project (speech band +7.8 dB
/// measured, +9.7 dB peak, hard constraint 10 dB). So:
///   * gain swing 0 → kDemoSpeechGainDb (8.0 dB, inside the measured/verified range, 2 dB margin)
///   * **the 24-band EQ is SONG_EQ only**. External-voice intent is a scalar intent; it must not be
///     written into the song's speech band, because that is exactly the substitution D0 forbids
///     (real-voice gain must not be expressed as a 1–4 kHz song EQ). An earlier demo revision did
///     write it there to make the panel move; that was a contract violation, not a feature.
///   * media ducks to kDemoMediaFloorPercent (60%): enough to expose dialogue while keeping the
///     programme present. The previous 25% floor was a strong demo value, not a model result.
constexpr float kDemoSpeechGainDb = 8.0f;
constexpr uint8_t kDemoMediaFloorPercent = 60;

enum class TuningMode : uint8_t {
    flat,
    song_eq,
    external_voice_intent,
    safety_hold,
};

enum class ConversationState : uint8_t {
    none,
    speech_nearby,
    likely,
    /// 预测到对话开始（媒体已预下压），但还没有确认；环境声尚未打开。
    anticipating,
};

enum class AdaptState : uint8_t {
    observing,
    adjusting,
    holding,
    recovering,
    mic_fault,
};

enum class EnvironmentKind : uint8_t {
    quiet,
    steady_noise,
    voice_likely,
    loud,
};

struct AnalysisFrame {
    uint32_t timestamp_ms = 0;
    float broadband_dbfs = -120.0f;
    float voice_likelihood = 0.0f;
    float motion = 0.0f;
    std::array<float, kBandCount> band_dbfs{};
    /// Center frequency of each band in Hz, same order as band_dbfs.
    std::array<float, kBandCount> band_center_hz{};
    bool mic_valid = false;
};

/// Measured descriptors of one analysis frame, exposed so the voice decision can be
/// inspected, unit-tested and re-calibrated without touching the DSP.
struct VoiceFeatures {
    /// Share of total band power falling inside the speech band (300-4000 Hz).
    float vocal_ratio = 0.0f;
    /// Least-squares slope of band level versus band index, in dB per band.
    /// Natural speech falls off towards high frequencies, so this is negative.
    float spectral_tilt_db_per_band = 0.0f;
    /// Largest single vocal-band share of the vocal-band power. Near 1.0 for a pure tone.
    float vocal_peakiness = 0.0f;
    /// Number of speech-band bands above the measurement floor. Speech excites several
    /// formant regions at once; a steady tone excites one.
    std::size_t excited_vocal_bands = 0;
    /// Geometric/arithmetic mean ratio of the band power spectrum. Near 1.0 for white noise.
    float spectral_flatness = 0.0f;
    /// Level gate in [0, 1]; low for frames too quiet to judge.
    float level_gate = 0.0f;
};

/// Extract the descriptors above from an already analysed frame.
VoiceFeatures extract_voice_features(const AnalysisFrame& frame);

/// Decide how voice-like a frame is, in [0, 1].
///
/// A single "share of energy in the speech band" test cannot separate voice from noise:
/// white noise puts roughly half its energy there, and any steady tone inside 300-4000 Hz
/// puts all of it there. Four descriptors are required:
///   * spectral tilt rejects white noise (flat spectrum, tilt near zero);
///   * vocal peakiness rejects steady tones (all vocal power in one band);
///   * excited-band count rejects narrowband signals that survive the tilt test;
///   * the level gate rejects frames too quiet to judge.
/// Thresholds are named constants in controller.cpp and are meant to be re-calibrated
/// against real recordings.
float voice_likelihood_from_features(const VoiceFeatures& features);

/// On-device listening-context model output (v0.2-s3, 92-dim window).
///
/// Written by the audio task right after `AdaptController::update()`, never by the controller
/// itself: Round 2 of `docs/integration-plan.md` brings the model on device as **telemetry**
/// (screen + serial JSON) and deliberately changes no decision. Round 3 is the round that may
/// wire `horn` into `safety_hold`, and it has to extend this contract on purpose.
///
/// Declared here rather than in `context_window.hpp` so `EarbudCommand` carries the model
/// result without the controller depending on the model header.
struct ContextResult {
    /// False until the 2 s window is full, and false again after an input interruption.
    bool valid = false;
    uint8_t class_index = 0;
    /// Softmax confidence of `class_index`, in (0, 1].
    float score = 0.0f;
    /// One forward pass, microseconds.
    int32_t inference_us = 0;
    /// Points at a string literal from `listening_model.h`: static storage, never freed.
    const char* class_name = "unknown";
};

/// 端侧声音事件判断（键盘 / 风 / 说话 / 安静 / 撞击）的输出。
///
/// 与 `ContextResult` 同一纪律：由音频任务写、`AdaptController` 从不读回；本轮只上报，
/// 不改任何决策。类别名指向 `sound_event.hpp` 的静态字符串，生命周期同进程。
struct SoundEventResult {
    /// False = 窗还没攒满，或刚被撞击清空。此时 `class_name` 不可当判断用。
    bool valid = false;
    const char* class_name = "quiet";
};

/// 自身语音证据的来源策略。
///
/// 三条路的**可信度与失败方式完全不同**，所以它必须是一个显式策略、并且要进遥测：
///   * `bone_only`：真机数据已否定骨传导判据（docs/apple-conversation-awareness.md 第 13 节），
///     所以这条路的实际表现是"不动作"——安全，但功能看不见；
///   * `near_field_demo`：用近场代理，演示时行为可见；代价是**旁人说话也会触发**（第 14 节），
///     遥测必须报 `near_field`，不允许说成自身语音；
///   * `disabled`：什么都不用，用于对比与排查。
enum class EvidencePolicy : uint8_t {
    bone_only,
    near_field_demo,
    disabled,
};

/// 自身语音证据。控制器只负责搬运与使用，判定在 main/bone_voice.hpp（骨传导）或
/// conversation::near_field_own_voice()（近场代理）。
///
/// 为什么要进遥测：真机验证时要能分别回答"骨传导有没有给出证据""给错的时候长什么样"，
/// 只看 media_percent 无法区分"证据没来"和"证据来了但状态机没动"。
struct OwnVoiceEvidence {
    /// IMU 的重力基线模长（m/s²）。近场代理时是 0（没有 IMU 数据）。
    float gravity = 0.0f;
    float confidence = 0.0f;
    float correlation = 0.0f;
    float vibration_rms = 0.0f;
    /// 整体运动否决是否生效。
    bool motion = false;
    /// true=骨传导证据。
    bool bone = false;
    /// 这一拍真的用了近场代理兜底（只在调用方显式打开开关时可能为真）。
    /// `bone=false && proxy=false` = 没有证据、fail-closed 不动作。遥测要能把这三种情况分开：
    /// 把"没证据"报成"用了代理"，就是在设备上撒一个我们查不到的谎。
    bool proxy = false;
    /// 这一拍 IMU 是否有数（false 时 bone 必然也是 false）。
    bool imu_ok = false;
};

struct EarbudCommand {
    /// v4 加了 `ambient_open`（环境声开放意图）。v3（自身语音证据）已上过机，所以新增
    /// 字段必须涨版本；上位机同时读得懂 v1–v4。
    uint32_t schema_version = 4;
    uint32_t sequence = 0;
    uint32_t timestamp_ms = 0;
    float speech_gain_db = 0.0f;
    float external_voice_gain_db = 0.0f;
    uint8_t media_percent = 100;
    /// 环境声开放意图。true = 对话已确认，应该让现实声音进入耳朵。
    ///
    /// demo 里由执行端（S3 麦克风中继 / 后续 AirPods 聆听模式）执行。**没有执行回执时
    /// 它只是意图**，面板必须把"意图"与"已执行"分开显示。
    bool ambient_open = false;
    // ── 判据遥测（只上报，不参与决策）────────────────────────────────────────────
    // 对话预判的训练与误触发排查都需要这些量；它们本来就在分析器里算过，不做二次计费。
    // 详见 docs/conversation-prediction.md。
    float broadband_dbfs = -120.0f;
    float voice_likelihood = 0.0f;
    float vocal_ratio = 0.0f;
    float spectral_flatness = 0.0f;
    float vocal_peakiness = 0.0f;
    uint8_t excited_vocal_bands = 0;
    std::array<float, kBandCount> eq_gain_db{};
    std::array<float, kBandCount> ambient_band_dbfs{};
    AdaptState state = AdaptState::observing;
    EnvironmentKind environment = EnvironmentKind::quiet;
    float confidence = 0.0f;
    bool earbud_link_connected = false;
    TuningMode tuning_mode = TuningMode::flat;
    ConversationState conversation = ConversationState::none;
    /// 自身语音证据（这一拍实际生效的那一份，含来源）。
    OwnVoiceEvidence own_voice;
    /// 产生这一帧时用的证据策略。遥测要报它，否则看数据的人无法判断
    /// `own_voice_source=near_field` 是"演示模式"还是"故障"。
    EvidencePolicy evidence_policy = EvidencePolicy::bone_only;
    /// Model telemetry. `AdaptController` never touches this field.
    ContextResult context;
    /// 声音事件判断（1 s 窗）。键盘/说话/撞击只上报；风在 song_eq 且无对话时压低频
    /// 抬升上限（D21，见 apply_wind_eq）。
    SoundEventResult event;
    /// 92 维窗上的"说话 vs 非说话"二分类学生（speech_gate_model.h）的分数与耗时。
    /// **只做遥测**，不参与决策；要替换四判据必须过 D19 验收（当前数据还不够）。
    float speech_gate_score = 0.0f;
    bool speech_gate_valid = false;
    int32_t speech_gate_us = 0;
};

// ── 风场景 EQ（D21）────────────────────────────────────────────────────────────
//
// 本 demo 没有降噪，执行端只有 EQ。风噪的能量集中在低频，此时继续把歌曲低频往上抬
// 既听不出提升、又会让整体更浑；所以风场景的策略是**给低频抬升设上限**，中高频不动。
// 这里只定义"风时怎么改形状"，不改变"是否放歌/是否下压"的决策：
//   * 只在 tuning_mode == song_eq（正在放歌）时生效；
//   * 对话优先（D21）：conversation != none 时 EQ 保持控制器给的原样，不叠加风策略；
//   * 低频段 = 前 5 个频段（70.7–451.7 Hz），因为它们装的是风噪/轰鸣的主要能量。
// 真机听感验证需要真实风环境，见 docs/decisions.md D21 的待办。
namespace wind_eq {
/// 风时低频段允许的最大抬升（dB）。控制器给 0 增益时不动，只压"抬得过高"的频段。
constexpr float kLowBandCapDb = 2.0f;
/// 低频段数量：0..4（70.7 / 141.4 / 244.9 / 346.4 / 451.7 Hz）。
constexpr std::size_t kLowBandCount = 5;
}  // namespace wind_eq

/// 把控制器的 EQ 形状按"有风"改一遍。纯函数，方便单测与离线复算。
inline void apply_wind_eq(EarbudCommand& command) {
    if (command.tuning_mode != TuningMode::song_eq) return;
    if (command.conversation != ConversationState::none) return;
    for (std::size_t band = 0; band < wind_eq::kLowBandCount; ++band) {
        if (command.eq_gain_db[band] > wind_eq::kLowBandCapDb) {
            command.eq_gain_db[band] = wind_eq::kLowBandCapDb;
        }
    }
}

/// Turns one analysis frame into an `AnalysisFrame`, including the voice decision.
///
/// **Stateful.** The per-frame voice decision alone cannot separate speech from a sustained
/// hum: a hum at f0 ~200 Hz has ~20 harmonics spread over 8-11 of the 24 bands, so the
/// peakiness gate (designed for a single steady tone) never fires and the per-frame product
/// reaches 1.00. Speech and hum differ in the *time* domain, so the analyzer keeps a short
/// history of two descriptors and applies a modulation gate on top of the per-frame score.
/// See `voice_thresholds::kMinLevelStdDb` in controller.cpp for the measured numbers.
class NoiseAnalyzer {
public:
    AnalysisFrame analyze(const int16_t* mono, std::size_t count, uint32_t timestamp_ms,
                          float motion = 0.0f);

    /// Drop the accumulated history. Call when the input stream is interrupted, so a gap
    /// is not mistaken for a steady sound (or for modulation that never happened).
    void reset();

private:
    /// ~1.5 s at the firmware's 100 ms control period.
    static constexpr std::size_t kHistoryFrames = 15;
    /// Frames required before the gate applies. Below this the returned score is exactly
    /// the per-frame product, which is what keeps single-frame callers (and their tests)
    /// behaving as before.
    static constexpr std::size_t kMinHistoryFrames = 5;

    /// Gate in [0, 1]; 1.0 when the sound moves the way speech does. Only ever lowers the
    /// score it is multiplied into.
    float modulation_term() const;

    std::array<float, kHistoryFrames> level_history_{};
    std::array<float, kHistoryFrames> ratio_history_{};
    std::size_t history_count_ = 0;
    std::size_t history_next_ = 0;
};

class AdaptController {
public:
    EarbudCommand update(const AnalysisFrame& frame, float dt_ms);
    void reset();

    /// 最近一次对话感知输出。工具与测试用它读 own_voice / 状态 / 已下压时长，
    /// 不必为了遥测往协议里加字段。
    const conversation::Output& awareness() const { return awareness_out_; }

    /// 由 app_main 每拍填入的**自身语音证据**（骨传导，来自 main/bone_voice.hpp）。
    ///
    /// 缺证据时的行为默认 **fail-closed**：没有骨传导证据就不下压。理由见 .cpp 里那段注释
    /// （代理在真机静音素材上 35/75 拍误判）。回放实验与需要旧行为的调用方显式打开开关。
    void set_own_voice_evidence(const OwnVoiceEvidence& evidence) { own_voice_evidence_ = evidence; }

    /// 证据来源策略。**默认 bone_only（fail-closed）**：没有骨传导证据就不下压。
    /// 演示模式由调用方显式打开，并且会如实反映在遥测的 own_voice_source 上。
    void set_evidence_policy(EvidencePolicy policy) { evidence_policy_ = policy; }
    EvidencePolicy evidence_policy() const { return evidence_policy_; }

private:
    uint32_t sequence_ = 0;
    float speech_gain_db_ = 0.0f;
    float media_percent_ = 100.0f;
    float valid_ms_ = 0.0f;
    float invalid_ms_ = 0.0f;
    /// Apple 式对话感知：自身语音触发 → 下压媒体 → 保持 → 恢复。
    /// 触发证据、时序参数与状态机都在 main/conversation_awareness.hpp 里；
    /// 旧实现的轮次启发式（语音→停顿→再语音）已删除，理由见 docs/apple-conversation-awareness.md。
    conversation::Awareness awareness_;
    conversation::Output awareness_out_{};
    OwnVoiceEvidence own_voice_evidence_{};
    EvidencePolicy evidence_policy_ = EvidencePolicy::bone_only;
};

const char* adapt_state_name(AdaptState state);
const char* environment_name(EnvironmentKind environment);
const char* tuning_mode_name(TuningMode mode);
const char* conversation_state_name(ConversationState state);
const char* evidence_policy_name(EvidencePolicy policy);
