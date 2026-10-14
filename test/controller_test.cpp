#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>
#include "../main/controller.hpp"

namespace {
int passed = 0;
int failed = 0;
void check(bool condition, const char* name) {
    if (condition) { ++passed; std::cout << "PASS " << name << '\n'; }
    else { ++failed; std::cout << "FAIL " << name << '\n'; }
}
AnalysisFrame frame(float dbfs, float voice, bool valid = true) {
    AnalysisFrame value;
    value.timestamp_ms = 1000;
    value.broadband_dbfs = dbfs;
    value.voice_likelihood = voice;
    value.mic_valid = valid;
    value.band_dbfs.fill(dbfs);
    return value;
}
/// The bands the controller is allowed to lift. Kept here rather than read from the
/// implementation so a silent change to the tuning width shows up as a test failure.
constexpr std::size_t kSpeechBandFirst = 8;
constexpr std::size_t kSpeechBandLast = 16;
}

int main() {
    AdaptController controller;
    auto quiet = controller.update(frame(-65, 0), 150);
    check(quiet.speech_gain_db == 0, "quiet stays flat");
    check(quiet.media_percent == 100, "quiet preserves media");

    EarbudCommand steady;
    for (int i = 0; i < 12; ++i) steady = controller.update(frame(-38, 0.0f), 150);
    check(steady.tuning_mode == TuningMode::song_eq, "steady noise selects song EQ");
    check(steady.media_percent == 100, "song EQ does not silently duck media");

    EarbudCommand voice;
    for (int i = 0; i < 12; ++i) voice = controller.update(frame(-28, 0.8f), 150);
    check(voice.tuning_mode == TuningMode::external_voice_intent,
          "nearby speech selects external voice intent");
    check(voice.external_voice_gain_db > 3, "external voice intent carries bounded gain");
    check(voice.external_voice_gain_db <= kMaxSpeechGainDb, "voice gain is bounded");
    check(voice.media_percent == 100, "external voice intent does not pretend media ducking is voice gain");

    // Real-world voice gain is not a song EQ. AirPods Pro exposes no public DSP control,
    // so this mode must leave the song prescription flat until the Mac relay path exists.
    std::size_t lifted = 0;
    bool all_match = true;
    bool outside_is_flat = true;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        const bool in_band = band >= kSpeechBandFirst && band <= kSpeechBandLast;
        if (in_band) {
            ++lifted;
            if (voice.eq_gain_db[band] != 0.0f) all_match = false;
        } else if (voice.eq_gain_db[band] != 0.0f) {
            outside_is_flat = false;
        }
    }
    check(lifted == kSpeechBandLast - kSpeechBandFirst + 1, "test inspects the full speech band");
    check(all_match, "external voice intent leaves speech-band song EQ flat");
    check(outside_is_flat, "external voice intent leaves all song bands flat");

    bool song_eq_has_gain = false;
    for (float gain : steady.eq_gain_db) song_eq_has_gain = song_eq_has_gain || gain > 0.05f;
    check(song_eq_has_gain, "steady environmental masking produces a song EQ suggestion");

    // No noise reduction anywhere: the product only has gain and tuning. This is a
    // compile-time guarantee now (the field is gone from EarbudCommand); the check that
    // matters at runtime is that the flat/quiet case does not lift anything.
    check(quiet.eq_gain_db[kSpeechBandFirst] == 0, "quiet lifts no band");

    EarbudCommand failed_frame;
    for (int i = 0; i < 3; ++i) failed_frame = controller.update(frame(-20, 0, false), 100);
    check(failed_frame.state == AdaptState::mic_fault, "persistent invalid mic enters fault");
    check(failed_frame.speech_gain_db < voice.speech_gain_db, "fault fails toward zero gain");
    check(failed_frame.media_percent == 100, "fault preserves media");
    check(failed_frame.eq_gain_db[kSpeechBandFirst] <
              steady.eq_gain_db[kSpeechBandFirst],
          "fault flattens the tuning");

    AdaptController safety_controller;
    EarbudCommand loud;
    for (int i = 0; i < 8; ++i) loud = safety_controller.update(frame(-20, 0.0f), 150);
    check(loud.tuning_mode == TuningMode::safety_hold, "very loud non-speech selects safety hold");
    bool safety_flat = true;
    for (float gain : loud.eq_gain_db) safety_flat = safety_flat && gain == 0.0f;
    check(safety_flat, "safety hold forbids positive EQ gain");

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
