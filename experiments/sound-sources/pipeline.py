#!/usr/bin/env python3
"""Overlapping human labels + exact S3 window vectors. No audio upload or auto labels."""
import argparse
import hashlib
import json
import wave
from pathlib import Path

import numpy as np

CLASSES = ["traffic", "horn_alarm", "speech", "wind", "music", "keyboard", "mechanical"]
FEATURE_VERSION = "s3-context-92-v1"
SPLITS = ("train", "validation", "test")


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def template(session):
    meta = json.loads((session / "meta.json").read_text())
    return dict(schema=1, session_id=session.name, source_id="", split="unassigned",
                feature_version=FEATURE_VERSION,
                provenance=dict(firmware_sha256="", git_revision="", date="", location="",
                                device_position="", playback="", recording_kind="real_s3"),
                time_origin="first_frame_ts_ms", origin_ms=meta["first_frame_ts_ms"],
                environment=[], annotations=[])


def audit(root):
    result = []
    for path in sorted(root.rglob("meta.json")):
        session = path.parent
        if not (session / "frames.jsonl").exists():
            continue
        meta = json.loads(path.read_text())
        vectors = session / "vectors.jsonl"
        exact = sum(r.get("valid") is True and r.get("sources_window_valid") is True
                    and r.get("feature_version") == FEATURE_VERSION for r in read_jsonl(vectors)) if vectors.exists() else 0
        result.append(dict(session=str(session), audio=(session / "audio.wav").exists(),
                           frames=meta.get("frames"), exact_vectors=exact,
                           human_annotations=(session / "annotations.json").exists(),
                           audio_gaps=len(meta.get("chunk_gaps", []))))
    return result


def validate_annotations(doc):
    if doc.get("schema") != 1 or doc.get("feature_version") != FEATURE_VERSION:
        raise ValueError("unsupported annotation schema/features")
    if doc.get("time_origin") != "first_frame_ts_ms" or type(doc.get("origin_ms")) is not int:
        raise ValueError("annotations need the first S3 frame timestamp")
    for key in ("session_id", "source_id"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise ValueError(f"missing {key}")
    if doc.get("split") not in SPLITS:
        raise ValueError("assign the entire original source/session to train, validation or test")
    provenance = doc.get("provenance", {})
    for key in ("firmware_sha256", "git_revision", "date", "location", "device_position", "playback"):
        if not isinstance(provenance.get(key), str) or not provenance[key].strip():
            raise ValueError(f"missing provenance.{key}")
    sha = provenance["firmware_sha256"]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("firmware_sha256 must identify the flashed artifact")
    if provenance.get("recording_kind") not in ("real_s3", "synthetic_fixture", "speaker_replay"):
        raise ValueError("recording_kind must be explicit")
    by_class = {name: [] for name in CLASSES}
    for item in doc["annotations"]:
        if item.get("class") not in by_class or type(item.get("value")) is not int or item["value"] not in (-1, 0, 1):
            raise ValueError("labels must use known classes and integer -1/0/1")
        a, b = item.get("start_s"), item.get("end_s")
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not np.isfinite([a, b]).all() or not 0 <= a < b):
            raise ValueError("invalid annotation interval")
        by_class[item["class"]].append((float(a), float(b), item["value"]))
    for intervals in by_class.values():
        intervals.sort()
        for i, (a, b, value) in enumerate(intervals):
            if any(c < b and d > a and value != other for c, d, other in intervals[i+1:]):
                raise ValueError("conflicting overlapping labels for the same source class")
    for item in doc.get("environment", []):
        if (item.get("label") not in ("road_traffic", "indoor", "other", "uncertain")
                or type(item.get("start_s")) not in (int, float)
                or type(item.get("end_s")) not in (int, float)
                or not np.isfinite([item["start_s"], item["end_s"]]).all()
                or not 0 <= item["start_s"] < item["end_s"]):
            raise ValueError("invalid environment annotation")
    return by_class


def label_window(intervals, start, end):
    # A boundary/partially reviewed window stays unknown, never becomes a negative.
    for value in (0, 1):
        cursor = start
        for a, b, v in intervals:
            if v != value or b <= cursor:
                continue
            if a > cursor + 1e-6:
                break
            cursor = max(cursor, b)
            if cursor >= end - 1e-6:
                return value
    return -1


def build_dataset(annotation_paths, out):
    if out.exists():
        raise ValueError("dataset already exists; choose a new output")
    rows = []
    docs = []
    for path in annotation_paths:
        doc = json.loads(path.read_text())
        labels = validate_annotations(doc)
        docs.append(doc)
        session = path.parent
        meta = json.loads((session / "meta.json").read_text())
        if meta.get("chunk_gaps") or meta.get("bad_lines", 0):
            raise ValueError(f"{session}: audio gaps/corrupt lines; recapture before labelling")
        if not (session / "audio.wav").exists() or meta.get("first_frame_ts_ms") != doc["origin_ms"]:
            raise ValueError(f"{session}: missing aligned audio/origin mismatch")
        with wave.open(str(session / "audio.wav"), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError(f"{session}: expected 16 kHz mono PCM16 reference audio")
            audio_seconds = audio.getnframes() / 16000
        audio_origin = meta.get("first_chunk_ts_ms")
        if type(audio_origin) is not int or audio_seconds <= 0:
            raise ValueError(f"{session}: missing audio timeline")
        frames = read_jsonl(session / "frames.jsonl")
        times = np.asarray([r["t_ms"] for r in frames], dtype=np.float64)
        if len(times) < 20 or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
            raise ValueError(f"{session}: invalid device frame timeline")
        indices = {int(t): i for i, t in enumerate(times)}
        seen = set()
        for record in read_jsonl(session / "vectors.jsonl"):
            if record.get("valid") is not True or record.get("sources_window_valid") is not True:
                continue
            if record.get("feature_version") != FEATURE_VERSION:
                raise ValueError("feature version mismatch")
            timestamp = record.get("t_ms")
            if type(timestamp) is not int or timestamp in seen:
                continue
            seen.add(timestamp)
            i = indices.get(timestamp, -1)
            if i < 19:
                continue
            window_times = times[i-19:i+1]
            if np.any((np.diff(window_times) < 50) | (np.diff(window_times) > 150)):
                continue
            x = np.asarray(record["vector"], dtype=np.float32)
            if x.shape != (92,) or not np.isfinite(x).all():
                raise ValueError("invalid exact S3 window vector")
            start = (window_times[0] - doc["origin_ms"]) / 1000
            end = (timestamp - doc["origin_ms"]) / 1000 + 0.1
            if (window_times[0] < audio_origin or
                    timestamp + 100 > audio_origin + audio_seconds*1000):
                continue
            y = [label_window(labels[c], start, end) for c in CLASSES]
            if all(v == -1 for v in y):
                continue
            rows.append((x, y, doc["source_id"], doc["session_id"], doc["split"],
                         start, end, doc["provenance"]["recording_kind"]))
    if not rows:
        raise ValueError("no fully reviewed exact S3 windows; old single labels are not multi-label truth")
    x, y, sources, sessions, splits, starts, ends, kinds = zip(*rows)
    dataset = dict(X=np.asarray(x), y=np.asarray(y, dtype=np.int8), classes=np.array(CLASSES),
                   source_id=np.array(sources), session_id=np.array(sessions), split=np.array(splits),
                   start_s=np.array(starts), end_s=np.array(ends), recording_kind=np.array(kinds),
                   feature_version=np.array(FEATURE_VERSION))
    validate_dataset(dataset)
    if out.with_suffix(".provenance.json").exists():
        raise ValueError("provenance output already exists; choose a new dataset path")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("xb") as handle:
        np.savez_compressed(handle, **dataset)
    write_json(out.with_suffix(".provenance.json"), dict(annotations=docs,
               annotation_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in annotation_paths}))
    return len(rows)


def validate_dataset(data):
    x, y = np.asarray(data["X"]), np.asarray(data["y"])
    n = len(x)
    if list(data["classes"]) != CLASSES or str(data["feature_version"]) != FEATURE_VERSION:
        raise ValueError("class order/feature version mismatch")
    if not n or x.shape != (n, 92) or x.dtype.kind not in "fiu" or not np.isfinite(x).all():
        raise ValueError("X must contain finite N x 92 S3 features")
    if y.shape != (n, 7) or y.dtype.kind not in "iu" or not np.isin(y, [-1, 0, 1]).all():
        raise ValueError("y must contain integer N x 7 labels -1/0/1")
    for key in ("source_id", "session_id", "split", "start_s", "end_s", "recording_kind"):
        if np.asarray(data[key]).shape != (n,):
            raise ValueError(f"{key} must have one entry per window")
    if not np.isfinite(data["start_s"]).all() or not np.isfinite(data["end_s"]).all():
        raise ValueError("non-finite window times")
    if np.any(np.asarray(data["start_s"]) < 0) or np.any(np.asarray(data["end_s"]) <= data["start_s"]):
        raise ValueError("invalid window times")
    if not set(data["split"]) <= set(SPLITS):
        raise ValueError("unknown split")
    if not set(data["recording_kind"]) <= {"real_s3", "synthetic_fixture", "speaker_replay"}:
        raise ValueError("unknown recording kind")
    for key in ("source_id", "session_id"):
        owners = {}
        for group, split in zip(data[key], data["split"]):
            if not str(group).strip() or owners.setdefault(str(group), str(split)) != str(split):
                raise ValueError(f"empty {key} or source/session leakage between splits")
    return x.astype(np.float32), y.astype(np.int8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("audit"); p.add_argument("root", type=Path)
    p = sub.add_parser("init"); p.add_argument("session", type=Path)
    p = sub.add_parser("build"); p.add_argument("annotations", nargs="+", type=Path); p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "audit":
            print(json.dumps(audit(args.root), ensure_ascii=False, indent=2))
        elif args.command == "init":
            write_json(args.session / "annotations.json", template(args.session))
        else:
            print(f"saved {build_dataset(args.annotations, args.out)} reviewed windows")
    except (KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
