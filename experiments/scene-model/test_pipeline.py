"""Check capture conversion and split discipline using synthetic fixtures, not field data."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('capture', ROOT / 'experiments/listening-model/scripts/collect_from_device.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class PipelineTest(unittest.TestCase):
    def test_capture_conversion_uses_real_broadband(self):
        bands = np.full((20, 24), -40.)
        times = np.arange(20) * .1
        real = np.full(20, -21.)
        frames = capture.frames_from_bands(bands, real, times)
        np.testing.assert_array_equal(frames['broadband_dbfs'], real)
        with self.assertRaises(ValueError):
            capture.frames_from_bands(bands, np.full(20, np.nan), times)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); session = root / 'near_voice-1'; session.mkdir()
            (session / 'meta.json').write_text(json.dumps({'label': 'near_voice'}))
            rows = [dict(t_ms=i*100, band_dbfs=bands[i].tolist(), broadband_dbfs=-21.) for i in range(20)]
            (session / 'frames.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            output = root / 'data.npz'
            capture.convert(root, output)
            with np.load(output, allow_pickle=False) as data:
                self.assertEqual(data['classes'].tolist(), ['near_voice'])
                self.assertAlmostEqual(float(data['X_train'][0, 72]), -21.)
            del rows[0]['broadband_dbfs']
            (session / 'frames.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            with self.assertRaises(SystemExit):
                capture.convert(root, output)

    def test_training_rejects_shared_recording_across_splits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / 'input.npz'
            np.savez(data, X=np.zeros((9, 92)), y=np.tile(np.arange(3), 3),
                     source_id=np.array(['same-recording']*9),
                     split=np.repeat(['train', 'validation', 'test'], 3),
                     classes=np.array(['street', 'highway', 'market']))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('train_model.py')),
                                     '--dataset', str(data), '--output', str(root/'model.json'),
                                     '--version', 'test-only'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('recording/source leakage', result.stderr)
            self.assertFalse((root/'model.json').exists())


if __name__ == '__main__':
    unittest.main()
