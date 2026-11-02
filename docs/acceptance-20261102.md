# 2026-11-02 consolidated candidate: acceptance protocol

Status: engineering candidate; deployment, listening quality and field accuracy
are not accepted yet. Use this protocol after the complete regression and a
coordinated device/service deployment. Do not auto-play music or upload speech
without the existing authorization covering that test.

## Candidate and branch decisions

Base: `feature/apple-conversation-awareness` at `49dc752` (matches local origin
reference at inspection). DS-1 changes through `46f5fad` were ported selectively,
not merged wholesale:

- Keep frequency-specific compensation, content-empty-band protection, observe
  estimation, transient gating on the matrix only and executor fingerprints.
- Keep current four-stem configuration, segment/window support and fallback.
- Omit the context/top2-based self-music-speech exemption: the nine-class model
  has not passed device validation.
- New scene output remains display/telemetry only; it is not injected into memo
  prompts for dialogue authorization.
- Basic positive ceiling 3 dB; aggressive 4 dB; doses 1.0/1.4. These are candidate
  parameters, not an accepted listening result. No global attenuation is added.

## Version and deployment record

Record Git revision and `git status`, systemeq SHA256, firmware binary SHA256,
flash log, host startup arguments, headphones/output route, room, device location,
track/segment and level for each test. Firmware does not report its build SHA;
use the recorded flash artifact and returned scene fields, not schema alone.
The host exposes `build_sha256`, requested/actual stem state, fallback reason,
worker `stem_bypass`, and estimated/applied leak state.

Before replacing any process:

1. Read `/api/snapshot` and save the active startup arguments without secrets.
2. Verify which process owns the serial port. Preserve its needed features.
3. Only after regression passes, stop the owner in a coordinated window, flash
   the recorded firmware and launch through `tools/host_up.sh --detach`, merging
   the saved parameters. Never use a bare host invocation.
4. Confirm new scene telemetry, matching executor SHA and fresh device frames.
   No fresh frame for 1 second must neutralize execution and close ambient audio.

If the snapshot or owner cannot be inspected, do not kill or flash blindly.

## Listening tests (user participation required)

Compare basic / aggressive / EQ off on identical track segments and headphone
volume. Compare with music attenuation alone for a genuine benefit claim.
Record clarity, timbre, pumping, glitches and clipping for each condition; do not
use changed frequency gains as a substitute for a listening result.

- Four stems: at least 30 minutes spanning sparse vocals, dense accompaniment
  and bass-heavy music; record underflow/drop counters, backlog, processing
  time/RTF and first sound delay. Any accumulating backlog or repeated audible
  interruption fails stability. User must confirm artifacts and delay acceptance.
- EQ off: check both EQ bypass and worker `stem_bypass=true`. Stem bypass uses
  original captured samples. Queued audio, one window and the overlap transition
  retain delay; it is not an immediate zero-latency raw path.
- Keyboard/background speech: compare masking and timbre across the three modes.
  No new audible roughness or repeated pumping is accepted without an explicit
  user decision. Fix only reproducible failures, then repeat the affected case.
- Leak: music alone in a quiet room, then add real external noise. Compare observe
  and apply with the same segment. Observe must calculate but never subtract;
  apply must not erase real noise. Keep default observe until A/B is accepted.

## Dialogue and speaker tests

At least three trials each: two-person conversation, wearer monologue, continuous
TV/podcast, and unrelated background babble. Mark wearer start/end and the actual
heard relay on/off times. Record device mode, authorization age/state,
`dialog_veto`, first response, missed/false opens and veto/close delay.

Acceptance for this candidate: every positive trial must open while the dialogue
is still active; every negative trial must remain closed. Expired/no/failed
semantic authorization must not open ambient audio. Report measured latency
without claiming an unagreed product timing target. TV/podcast semantic veto
must reach the device; context alone never authorizes output.

Speaker identity: test the current enrollment first; if inadequate, collect
three voice conditions on two days. Report wearer/other/noise mistakes separately.
Keep online learning off. Single-day success is not cross-day acceptance.

## Data collection (not yet performed)

Use S3 features, not Mac microphone replacements. Cover nine context labels:
quiet / steady / street / horn / babble / near_voice / music / wind / other.
Store original source ID, session ID/date, room, distance, playback device/level
and human label. An initial 30-minute session starts the dataset; it does not
complete model training or field acceptance.

The listening-model collector now stores real `broadband_dbfs` and rejects
conversion if missing; motion remains zero. Converted windows are unpartitioned
training candidates, not an independent test set. Split entire original sources
and sessions before any new training. Scene-head training uses a separate,
source-disjoint NPZ contract documented in `experiments/scene-model/README.md`.

## Rollback

Preserve the pre-integration bundle/archive outside the repository. Do not reset
shared checkouts or force-push. `--no-stem-remix` restores the non-stem execution
path, while retaining the consolidated tuning candidate; it is not a full code
rollback. To restore exact old behavior, use the saved revision in an isolated
checkout and rebuild its executor/firmware, then redeploy with the saved startup
arguments after coordinating serial ownership.
