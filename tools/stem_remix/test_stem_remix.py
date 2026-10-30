import math
import unittest

from tools.stem_remix.stem_remix import (
    STEMS,
    StemStats,
    automatic_profile,
    dominant_roles,
    role_profile,
    contrast_only_profile,
    remix_risk,
    smooth_role_windows,
)


class RolePolicyTest(unittest.TestCase):
    def setUp(self):
        levels = {
            "vocals": -12.0,
            "drums": -10.0,
            "bass": -15.0,
            "guitar": -18.0,
            "piano": -24.0,
            "other": -20.0,
        }
        self.stats = {
            stem: StemStats(rms_dbfs=levels[stem], peak_dbfs=-3.0)
            for stem in STEMS
        }

    def test_dominant_roles_are_selected_from_role_candidates(self):
        roles = dominant_roles(self.stats)
        self.assertEqual(roles["lead"], "vocals")
        self.assertEqual(roles["rhythm"], "drums")
        self.assertEqual(roles["low"], "drums")
        self.assertEqual(roles["bed"], "guitar")

    def test_auto_profile_protects_lead_and_keeps_bounded_gains(self):
        gains = automatic_profile(self.stats)
        self.assertEqual(gains["vocals"], 3.0)
        self.assertGreaterEqual(gains["drums"], 1.0)
        self.assertEqual(set(gains), set(STEMS))
        self.assertTrue(all(math.isfinite(value) for value in gains.values()))
        self.assertTrue(all(-1.0 <= value <= 3.0 for value in gains.values()))

    def test_environment_policy_uses_roles_not_genre_names(self):
        roles = dominant_roles(self.stats)
        wind = role_profile(roles, "wind")
        babble = role_profile(roles, "babble")
        quiet = role_profile(roles, "quiet")
        self.assertEqual(wind["bass"], -1.0)
        self.assertEqual(babble["vocals"], 3.5)
        self.assertTrue(all(value == 0.0 for value in quiet.values()))

    def test_role_hysteresis_rejects_one_window_jitter(self):
        windows = [self.stats]
        jitter = dict(self.stats)
        jitter["guitar"] = StemStats(rms_dbfs=-11.0, peak_dbfs=-3.0)
        windows.append(jitter)
        windows.append(self.stats)
        roles = smooth_role_windows(windows, margin_db=0.5, hold_windows=2)
        self.assertTrue(all(window["lead"] == "vocals" for window in roles))

    def test_contrast_policy_never_boosts_and_risk_returns_to_original(self):
        roles = dominant_roles(self.stats)
        requested = role_profile(roles, "broadband")
        safe = contrast_only_profile(requested, strength=0.6, risk=0.0)
        bypass = contrast_only_profile(requested, strength=0.6, risk=1.0)
        self.assertEqual(max(safe.values()), 0.0)
        self.assertTrue(all(value <= 0.0 for value in safe.values()))
        self.assertTrue(all(value == 0.0 for value in bypass.values()))

    def test_dry_isolated_transient_is_safer_than_dense_wet_transient(self):
        dry = remix_risk(crest_db=19.0, active_stems=2, vocal_side_ratio=0.05)
        wet = remix_risk(crest_db=19.0, active_stems=6, vocal_side_ratio=0.5)
        self.assertLess(dry, wet)
        self.assertGreater(wet, 0.7)


if __name__ == "__main__":
    unittest.main()
