#include <cmath>
#include <cstdlib>
#include <iostream>
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
}

int main() {
    AdaptController controller;
    auto quiet = controller.update(frame(-65, 0), 150);
    check(quiet.speech_gain_db == 0, "quiet stays flat");
    check(quiet.media_percent == 100, "quiet preserves media");

    EarbudCommand noisy;
    for (int i = 0; i < 12; ++i) noisy = controller.update(frame(-28, 0.8f), 150);
    check(noisy.speech_gain_db > 3, "noisy voice raises speech target");
    check(noisy.noise_reduction_db > 2, "noisy voice requests noise reduction");
    check(noisy.media_percent < 100, "voice in noise yields media");
    check(noisy.speech_gain_db <= kMaxSpeechGainDb, "speech gain is bounded");
    check(noisy.noise_reduction_db <= kMaxNoiseReductionDb, "noise reduction is bounded");

    EarbudCommand failed_frame;
    for (int i = 0; i < 3; ++i) failed_frame = controller.update(frame(-20, 0, false), 100);
    check(failed_frame.state == AdaptState::mic_fault, "persistent invalid mic enters fault");
    check(failed_frame.speech_gain_db < noisy.speech_gain_db, "fault fails toward zero gain");
    check(failed_frame.media_percent > noisy.media_percent, "fault restores media");

    check(std::string(adapt_state_name(AdaptState::adjusting)) == "ADJUSTING",
          "state names are stable protocol values");
    std::cout << "Controller: " << passed << "/" << (passed + failed) << " passed\n";
    return failed == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
