# S3 multi-source recognition (experimental, telemetry only)

The old nine-class context model and one-label event rules cannot express
coexisting sources. `context_top2` is a runner-up in an exclusive softmax, not
evidence that a second source exists. This experiment adds seven independent
sigmoid channels using the actual S3 2-second, 92-feature window:

| Channel | Meaning |
| --- | --- |
| `traffic` | Moving road traffic, including passing vehicles |
| `horn_alarm` | Audible horns or alarms; not an exhaustive hazard detector |
| `speech` | Audible speech; no identity, distance or dialogue claim |
| `wind` | Wind at the microphone, including turbulent wind noise |
| `music` | Audible music, including playback leakage |
| `keyboard` | Keyboard typing |
| `mechanical` | Steady mechanical hum, such as fan/AC/motor noise |

Traffic and mechanical hum may coexist. Source labels are independent of human
environment annotations (`road_traffic`, `indoor`, `other`, `uncertain`). These
environment annotations are retained in dataset provenance; this experiment
does not install another automatic environment classifier.

Unknown and unreviewed labels are not negatives. `quiet` is reserved as an
independently calibrated low-level state and is currently null. No detections
does not mean silence. `artifact` belongs to input validity, not the source list.

## Current delivery and data gap

The inference, collection, annotation, dataset, training, export and panel paths
are implemented. `main/sound_sources_model.h` deliberately returns no model until
real overlapping labels are available. The panel therefore reports a model
awaiting training; this is not a field-capable recognition release.

2026-11-02 inventory: four session directories in the tracked sound-events data
tree and ten in local `记录/` have metadata/frame logs. Some are copies of the
same recordings, not independent sources. None contains the new exact-vector
records or seven-channel human annotations. Two local voiceprint sessions also
report audio gaps. Existing phase names and teacher predictions are not promoted
to human multi-label ground truth. Older audio can help humans design labels,
but does not satisfy this exact-window dataset contract.

## Collect and annotate

Coordinate the existing serial owner first: inspect the live snapshot/startup
flags and preserve the running demo configuration. This collector needs exclusive
serial access. Do not stop another chat's host blindly. It does not call ASR,
upload audio, play cues by default or flash firmware. A matching firmware build
must already be installed, and the existing 16 kHz serial audio throughput still
requires a real capture check. The audio is a reference for annotation, not a
replacement for the 48 kHz-derived S3 feature vector.

```sh
python3 tools/collect_teacher_data.py --port /dev/cu.usbmodem101 \
  --seconds 120 --window-vectors --out 记录/混合声源/road-day1-session1
python3 experiments/sound-sources/pipeline.py init 记录/混合声源/road-day1-session1
```

Use an interpreter with pyserial for capture and NumPy for dataset/training.
The vector collector polls the existing `vector` console command every 0.5 s.
It creates a new directory, saves aligned `audio.wav`, `frames.jsonl`,
`vectors.jsonl`, and `meta.json`, and refuses to overwrite a vector capture.
The vector includes `feature_version=s3-context-92-v1` and
`sources_window_valid=true` only after 20 contiguous valid device frames.
Each cadence must be 50-150 ms; gaps, clipping and restart require a fresh window.

Fill `annotations.json` with the actual firmware binary SHA256, Git revision,
date, location, S3 placement and playback state (`none` is an explicit state).
Assign `source_id` to the original recording/source, including all of its
excerpts and replays. `session_id` identifies the physical capture session.
Assign the complete source/session to `train`, `validation` or `test` before
training. Use `real_s3` only for real acoustic sources recorded by S3;
speaker playback uses `speaker_replay`, and tests use `synthetic_fixture`.

Annotation times are seconds relative to `first_frame_ts_ms`, not WAV sample zero.
Convert listening position with:
`annotation_s = wav_s + (first_chunk_ts_ms - first_frame_ts_ms)/1000`.
Review each source separately and mark both positives and negatives explicitly:

```json
{
  "environment": [{"start_s": 0, "end_s": 30, "label": "road_traffic"}],
  "annotations": [
    {"start_s": 0, "end_s": 30, "class": "traffic", "value": 1},
    {"start_s": 8, "end_s": 12, "class": "speech", "value": 1},
    {"start_s": 0, "end_s": 8, "class": "speech", "value": 0},
    {"start_s": 8, "end_s": 12, "class": "wind", "value": 1},
    {"start_s": 0, "end_s": 30, "class": "music", "value": 0}
  ]
}
```

This fragment goes into the generated template, which supplies required metadata.
`1` = present, `0` = reviewed absent, `-1` = uncertain; missing coverage is also
unknown. A 2 s window must have complete, consistent coverage for a class to
receive a 0 or 1 label. Different classes can overlap; contradictory overlaps
within one class are rejected. Window boundary transitions remain unknown.

## Build, train, export

```sh
python3 experiments/sound-sources/pipeline.py audit 记录
python3 experiments/sound-sources/pipeline.py build \
  记录/混合声源/road-day1-session1/annotations.json \
  记录/混合声源/road-day2-session1/annotations.json \
  记录/混合声源/road-day3-session1/annotations.json \
  --out work/sound-sources/dataset-v1.npz
python3 experiments/sound-sources/train_model.py \
  --dataset work/sound-sources/dataset-v1.npz \
  --output work/sound-sources/model-v1.json --version v0.1-s3-sources
python3 experiments/sound-sources/export_model.py \
  --model work/sound-sources/model-v1.json \
  --output work/sound-sources/staging/sound_sources_model.h
```

The three example sessions are a format example, not sufficient acceptance
coverage. Use more independent sources/sessions for the test minimums below.
NPZ contains `X: N x 92`, integer `y: N x 7` (-1/0/1), class order, feature
version, source/session IDs, explicit split, window start/end and recording kind.
The adjacent provenance JSON preserves source annotations and their hashes.
Missing exact vectors, audio reference, metadata, corrupt/gapped capture and
source/session leakage are rejected. Nothing automatically splits adjacent
windows or assigns negatives to unreviewed sounds.

Training uses NumPy float32, seed 7, 500 full-batch Adam steps, a ReLU hidden
layer (92 -> 24 -> 7) and masked binary cross-entropy gradients. Normalization
and weights use train only; per-class thresholds use validation only. A class
requires both positive and negative labels in train and validation, otherwise
its output stays unavailable. The threshold grid is 0.05-0.95 in 0.01 steps:
prefer precision >=80% and FPR <=10%, maximize recall, then lower FPR and higher
threshold; if no candidate qualifies, maximize F1 and retain failure status.

Saved artifacts include parameters, thresholds, masks, dataset SHA256, dependency
version, per-class reports and golden vectors. Export is standard-library-only,
refuses overwrite and writes to staging. `--check` compares an existing header
without changing it. It never trains or marks any class field validated.
Integrating an artifact into firmware is a separate reviewed change.

## Interface and acceptance

An optional `sound_sources` object is appended to schema-4 `earbud_command`:
`schema:1`, `version`, `status`, `valid`, `inference_us`, `quiet:null`, seven
`scores` in the class order above, `detected_mask`, `validated_mask` (bit 0 is
traffic). Scores can be null for unsupported classes. They are model scores,
not calibrated probabilities; detection bits are authoritative at rounded
serialization boundaries. Mask bits for null scores are invalid.

Statuses: `window_incomplete`, `input_invalid`, `model_unavailable`,
`model_invalid`, `ready`. The host validates the object, expands named channels,
and clears current detections on stale frames, disconnect or missing/malformed
telemetry. Old firmware remains readable. New results are never read by EQ,
speaker identity, semantic authorization, memo or dialogue policy; old `event`
and control outputs remain on their existing paths.

Reports split all, mixed (at least two known positive sources) and fully reviewed
single-source windows. Each class needs real S3 test precision >=80%, recall
>=80%, negative-window FPR <=10%, >=60 s positive coverage, >=60 s negative
coverage and >=3 independent test sessions, both overall and in mixtures.
Overlapping windows count once toward duration. Unknown labels are excluded.
Synthetic/speaker replay cases never contribute to real-S3 acceptance.
Reports use `data_insufficient`, `offline_failed`, `offline_pass`; even an
offline pass keeps `field_validated=false` until independent device validation.

Before deployment, measure added inference <=2 ms/frame and no extra capture
loss, confirm serial bandwidth, then test gap/clipping/restart recovery and
unchanged control outputs on matching replay. None of these device measurements
has been completed by the synthetic tests.

```sh
cmake -S test -B build-host-tests
cmake --build build-host-tests --parallel
ctest --test-dir build-host-tests --output-on-failure
python3 experiments/sound-sources/test_pipeline.py
python3 tools/host/tests/test_host.py
sh tools/build.sh
```
