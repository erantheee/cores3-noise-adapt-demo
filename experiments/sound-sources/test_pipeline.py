#!/usr/bin/env python3
"""Synthetic implementation tests, never S3 accuracy evidence."""
import copy
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from export_model import render
from pipeline import (CLASSES, FEATURE_VERSION, build_dataset, label_window, template,
                      validate_annotations, validate_dataset)
from train_model import duration, fit

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    rng = np.random.default_rng(31)
    n = 180
    x = rng.normal(size=(n, 92)).astype(np.float32)
    y = (x[:, :7] > 0).astype(np.int8)
    y[:, 6] = -1
    return dict(X=x, y=y, classes=np.array(CLASSES), feature_version=np.array(FEATURE_VERSION),
                split=np.repeat(["train", "validation", "test"], 60),
                source_id=np.repeat(["source-a", "source-b", "source-c"], 60),
                session_id=np.repeat(["session-a", "session-b", "session-c"], 60),
                start_s=np.tile(np.arange(60)*0.5, 3), end_s=np.tile(np.arange(60)*0.5+2, 3),
                recording_kind=np.repeat("synthetic_fixture", n))


class PipelineTests(unittest.TestCase):
    def test_build_exact_overlapping_labels_and_preserve_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = dict(first_frame_ts_ms=1000, first_chunk_ts_ms=1000, chunk_gaps=[], bad_lines=0)
            (root / "meta.json").write_text(json.dumps(meta))
            frames = [dict(t_ms=1000 + i*100) for i in range(40)]
            (root / "frames.jsonl").write_text("\n".join(json.dumps(r) for r in frames))
            vectors = [dict(valid=True, sources_window_valid=True, feature_version=FEATURE_VERSION,
                            t_ms=t, vector=[0.0]*92) for t in (2900, 3400)]
            (root / "vectors.jsonl").write_text("\n".join(json.dumps(r) for r in vectors))
            with wave.open(str(root / "audio.wav"), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b"\0\0"*64000)
            doc = template(root)
            doc.update(source_id="original-fixture", split="train")
            doc["provenance"] = dict(firmware_sha256="a"*64, git_revision="fixture", date="2026-11-02",
                                     location="fixture", device_position="fixture", playback="none",
                                     recording_kind="synthetic_fixture")
            doc["annotations"] = [dict(start_s=0, end_s=4, **{"class":c}, value=1)
                                  for c in ("traffic", "speech", "wind")]
            doc["annotations"].append(dict(start_s=0, end_s=4, **{"class":"music"}, value=0))
            annotations = root / "annotations.json"
            annotations.write_text(json.dumps(doc))
            out = root / "dataset.npz"
            self.assertEqual(build_dataset([annotations], out), 2)
            with np.load(out, allow_pickle=False) as data:
                self.assertEqual(data["y"][0].tolist(), [1, -1, 1, 1, 0, -1, -1])
            with self.assertRaises(ValueError):
                build_dataset([annotations], out)
            bad = copy.deepcopy(doc)
            bad["annotations"].append(dict(start_s=1, end_s=2, **{"class":"speech"}, value=0))
            with self.assertRaises(ValueError):
                validate_annotations(bad)
            meta["chunk_gaps"] = [[2, 4]]
            (root / "meta.json").write_text(json.dumps(meta))
            with self.assertRaises(ValueError):
                build_dataset([annotations], root / "bad.npz")

    def test_overlap_boundaries_and_unknown(self):
        self.assertEqual(label_window([(0, 2, 1), (2, 4, 1)], 1, 3), 1)
        self.assertEqual(label_window([(0, 2, 1)], 1, 3), -1)
        self.assertEqual(label_window([(0, 2, 0)], 0, 2), 0)
        self.assertEqual(label_window([(0, 2, -1)], 0, 2), -1)

    def test_reject_leakage_float_labels_and_bad_timeline(self):
        data = fixture()
        validate_dataset(data)
        for key in ("session_id", "source_id"):
            bad = copy.deepcopy(data)
            bad[key][:] = "same"
            with self.assertRaises(ValueError):
                validate_dataset(bad)
        bad = dict(data, y=data["y"].astype(float))
        with self.assertRaises(ValueError):
            validate_dataset(bad)
        with self.assertRaises(ValueError):
            validate_dataset(dict(data, end_s=data["start_s"]))
        self.assertEqual(duration(data, data["split"] == "test"), 31.5)

    def test_fit_masks_unknown_and_uses_no_test_labels(self):
        data = fixture()
        artifact = fit(data, epochs=60)
        altered = copy.deepcopy(data)
        test = altered["split"] == "test"
        altered["y"][test, :6] = 1-altered["y"][test, :6]
        second = fit(altered, epochs=60)
        for key in ("normalization", "w1", "w2", "b1", "b2", "thresholds"):
            self.assertEqual(artifact[key], second[key])
        self.assertFalse(artifact["trained"][6])
        self.assertTrue(all(not v for v in artifact["field_validated"]))
        self.assertTrue(all(s == "data_insufficient" for s in artifact["acceptance"].values()))
        artifact["version"] = "synthetic-test"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "model.h").write_text(render(artifact))
            cases = artifact["golden"]
            source = ['#include <cstdio>', '#include "model.h"', 'int main() {']
            for case in cases:
                values = ",".join(format(float(v), ".9g") for v in case["vector"])
                source += ['{ float x[92] = {' + values + '};',
                           'auto r = sound_sources::evaluate(sound_sources_model::get(), x);',
                           'if (!r.valid) return 2;',
                           'for (int c=0;c<7;++c) std::printf("%.9g ", r.scores[c]);',
                           'std::printf("\\n"); }']
            source += ['}']
            (path / "check.cpp").write_text("\n".join(source))
            subprocess.run(["c++", "-std=c++17", "-I", str(ROOT / "main"), str(path / "check.cpp"),
                            "-o", str(path / "check")], check=True, capture_output=True)
            output = subprocess.check_output([str(path / "check")], text=True)
            actual = np.array([[float(v) for v in line.split()] for line in output.splitlines()])
            expected = np.array([case["scores"] for case in cases])
            expected[:, 6] = 0
            np.testing.assert_allclose(actual, expected, atol=2e-5, rtol=2e-5)
        invalid = copy.deepcopy(artifact)
        invalid["field_validated"][0] = True
        with self.assertRaises(ValueError):
            render(invalid)


if __name__ == "__main__":
    unittest.main()
