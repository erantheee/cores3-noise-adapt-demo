#!/usr/bin/env python3
"""Independent telemetry must never alter existing command/control fields."""
import json
import sys
import time
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.protocol import CommandFrame, parse_line
from host.sound_sources import parse_sources, snapshot_sources
from host.state import HostState


def telemetry():
    return dict(schema=1, status="ready", version="synthetic-test", valid=True, inference_us=20,
                quiet=None, scores=[0.9, 0.1, 0.9, 0.9, 0.1, 0.1, None],
                detected_mask=13, validated_mask=0)


class SourceTests(unittest.TestCase):
    def test_independent_channels_and_clear_on_disconnect(self):
        raw = telemetry()
        parsed = parse_sources(raw)
        self.assertEqual(sum(v["detected"] is True for v in parsed["channels"].values()), 3)
        stale = snapshot_sources(parsed, False)
        self.assertFalse(stale["valid"])
        self.assertTrue(all(v["detected"] is None for v in stale["channels"].values()))
        self.assertTrue(parsed["valid"])
        self.assertIsNone(snapshot_sources(None, True))

    def test_malformed_channels_and_nonfinite_scores(self):
        for value in (None, [], "invalid-status", True):
            raw = telemetry()
            raw["status"] = value
            with self.assertRaises(ValueError):
                parse_sources(raw)
        for bad in (True, float("nan"), float("inf"), -0.1, 1.1, "0.7"):
            raw = telemetry()
            raw["scores"][2] = bad
            with self.assertRaises(ValueError):
                parse_sources(raw)
        raw = telemetry()
        raw["detected_mask"] |= 64
        with self.assertRaises(ValueError):
            parse_sources(raw)
        raw = telemetry()
        raw["status"] = "input_invalid"
        raw["valid"] = False
        with self.assertRaises(ValueError):
            parse_sources(raw)

    def test_collector_saves_device_vectors_and_refuses_overwrite(self):
        import tempfile
        from collect_teacher_data import Capture
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "session"
            recorder = Capture(dest, window_vectors=True)
            vector = dict(type="window_vector", valid=True, sources_window_valid=True,
                          t_ms=1900, feature_version="s3-context-92-v1", vector=[0.0]*92)
            recorder.feed(json.dumps(vector).encode())
            meta = recorder.close(2)
            self.assertEqual(meta["window_vectors"], 1)
            self.assertEqual(json.loads((dest / "vectors.jsonl").read_text()), vector)
            with self.assertRaises(ValueError):
                Capture(dest, window_vectors=True)
    def test_legacy_frame_and_unchanged_controls(self):
        fixture = Path(__file__).parent / "fixtures/device_real.log"
        payload = next(json.loads(line) for line in fixture.read_text().splitlines()
                       if line.startswith('{"type":"earbud_command"'))
        base = parse_line(json.dumps(payload)).value
        self.assertIsInstance(base, CommandFrame)
        for sources in (telemetry(), {"status": "broken"}):
            candidate = parse_line(json.dumps(dict(payload, sound_sources=sources))).value
            a, b = asdict(base), asdict(candidate)
            a.pop("sound_sources"); b.pop("sound_sources")
            self.assertEqual(a, b)
        state = HostState()
        state.connected = True
        candidate = parse_line(json.dumps(dict(payload, sound_sources=telemetry()))).value
        state.feed_command(candidate)
        self.assertTrue(state.snapshot()["frame"]["sound_sources"]["valid"])
        state.last_frame_at = time.monotonic() - 60
        self.assertFalse(state.snapshot()["frame"]["sound_sources"]["valid"])


if __name__ == "__main__":
    unittest.main()
