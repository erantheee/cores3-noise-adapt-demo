"""Component tests only: these do not validate real-time audio or listening quality."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stream_worker import controlled_settings, mix_stems, passthrough_window, stats_for


class StreamWorkerTest(unittest.TestCase):
    def test_four_stems_do_not_invent_guitar_or_piano(self):
        original = torch.ones(2, 128) * .2
        stems = {k: original / 4 for k in ('vocals', 'drums', 'bass', 'other')}
        stats = stats_for(stems)
        self.assertEqual(stats['guitar'].rms_dbfs, -120)
        self.assertEqual(stats['piano'].rms_dbfs, -120)
        torch.testing.assert_close(mix_stems(original, stems, {k: 0 for k in stats}), original)
        # Missing tracks cannot alter the mix even if a shared policy gives them gain.
        torch.testing.assert_close(mix_stems(original, stems,
                                            {k: 6 if k in ('guitar', 'piano') else 0 for k in stats}),
                                   original)

    def test_bypass_preserves_captured_samples(self):
        raw = np.linspace(-1, 1, 64, dtype=np.float32).reshape(-1, 2)
        processed = raw * .4
        np.testing.assert_array_equal(passthrough_window(raw, processed, True), raw)
        np.testing.assert_array_equal(passthrough_window(raw, processed, False), processed)

    def test_environment_updates_preserve_bypass_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'control.json'
            path.write_text(json.dumps({'environment': 'wind', 'bypass': True}))
            self.assertEqual(controlled_settings(str(path), 'quiet'), ('wind', True))
            path.write_text('{broken')
            self.assertEqual(controlled_settings(str(path), 'quiet'), ('quiet', False))


if __name__ == '__main__':
    unittest.main()
