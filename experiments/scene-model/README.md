# S3 scene head — experimental, telemetry only

The three-class learned head predicts street / highway / market from the existing
92-feature S3 window. Quiet, speech and other labels come from the surrounding
rules. This does not replace the nine-class context model and does not authorize
EQ, ambient audio or dialogue decisions.

## Reproduce the current model

The original export tool lived outside this repository and trained as part of
export. Its final fit included every source window, including the rows called
validation. Existing golden cases therefore prove implementation equivalence
only; they are not independent accuracy evidence.

`artifacts/v0.1-s3-scene.json` preserves the existing folded float32 weights,
bias, feature normalization, class order, golden inputs and dataset checksum.
The original unfused checkpoint was not saved; this is a recovery of the shipped
parameters, not a claim that the old training run is exactly reproducible.
Raw recordings and the source dataset remain local and are not published.

Export needs Python standard library only and never trains:

```sh
python3 experiments/scene-model/export_model.py \
  --model experiments/scene-model/artifacts/v0.1-s3-scene.json --output-root .
python3 experiments/scene-model/export_model.py \
  --model experiments/scene-model/artifacts/v0.1-s3-scene.json --output-root . --check
```

The check runs in CTest/CI. It checks the artifact against both generated headers;
`scene_test` separately checks float32 logits (absolute tolerance 0.02), labels
and wrapper rules. Neither test proves field classification quality.

## Train a new version

Training is separate, CPU deterministic (seed 7, 500 epochs), using NumPy and
PyTorch (pipeline tests here used NumPy 2.4.6 / PyTorch 2.15.0). Save the dependency
versions in the artifact. Input NPZ must contain:

- `X`: finite N×92 S3 features; `y`: integer labels 0..2.
- `classes`: street, highway, market, in exactly that order.
- `source_id`: original recording/session identifier per row. Replayed excerpts
  of one recording must share this ID even when captured in separate sessions.
- `split`: train / validation / test per row, with all classes in each split.

The trainer rejects shared source IDs across splits, fits normalization and
weights on train only, and reports confusion matrices and per-class precision,
recall and F1 separately for each split. It refuses to overwrite an artifact.

```sh
python3 experiments/scene-model/train_model.py --dataset /path/to/s3-scenes.npz \
  --output /path/to/new-model.json --version v0.2-s3-scene
python3 experiments/scene-model/export_model.py --model /path/to/new-model.json \
  --output-root /path/to/staging
python3 experiments/scene-model/test_pipeline.py
```

Export to staging first. A new artifact is not automatically installed or enabled
for decisions. A new model still requires independent recordings, full wrapper
checks, firmware measurements and real S3 microphone validation. The current
pipeline tests use synthetic fixtures and do not count as collected data.
