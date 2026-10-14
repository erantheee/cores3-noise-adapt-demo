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

    EarbudCommand noisy;
    for (int i = 0; i < 12; ++i) noisy = controller.update(frame(-28, 0.8f), 150);
    check(noisy.speech_gain_db > 3, "noisy voice raises speech target");
    check(noisy.media_percent < 100, "voice in noise yields media");
    check(noisy.speech_gain_db <= kMaxSpeechGainDb, "speech gain is bounded");

    // Tuning is the product's second action, and it is what gets sent to the earbud.
    // The speech band must be lifted by exactly the scalar gain, and every other band must
    // stay at 0 dB -- if a neighbour band drifts up, the tuning is no longer what the
    // device screen and the host panel both claim it is.
    std::size_t lifted = 0;
    bool all_match = true;
    bool outside_is_flat = true;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        const bool in_band = band >= kSpeechBandFirst && band <= kSpeechBandLast;
        if (in_band) {
            ++lifted;
            if (std::fabs(noisy.eq_gain_db[band] - noisy.speech_gain_db) > 1e-4f) all_match = false;
        } else if (noisy.eq_gain_db[band] != 0.0f) {
            outside_is_flat = false;
        }
    }
    check(lifted == kSpeechBandLast - kSpeechBandFirst + 1, "tuning lifts exactly the speech band");
    check(all_match, "every lifted band equals the reported speech gain");
    check(outside_is_flat, "bands outside the speech band stay at 0 dB");
    check(noisy.eq_gain_db[kSpeechBandFirst] > 0, "the speech band is actually lifted");

    // No noise reduction anywhere: the product only has gain and tuning. This is a
    // compile-time guarantee now (the field is gone from EarbudCommand); the check that
    // matters at runtime is that the flat/quiet case does not lift anything.
    check(quiet.eq_gain_db[kSpeechBandFirst] == 0, "quiet lifts no band");

    EarbudCommand failed_frame;
    for (int i = 0; i < 3; ++i) failed_frame = controller.update(frame(-20, 0, false), 100);
    check(failed_frame.state == AdaptState::mic_fault, "persistent invalid mic enters fault");
    check(failed_frame.speech_gain_db < noisy.speech_gain_db, "fault fails toward zero gain");
    check(failed_frame.media_percent > noisy.media_percent, "fault restores media");
    check(failed_frame.eq_gain_db[kSpeechBandFirst] <
              noisy.eq_gain_db[kSpeechBandFirst],
          "fault flattens the tuning");

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
