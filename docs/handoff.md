# Session handoff — 2026-11-02

## Candidate
- Branch: `feature/apple-conversation-awareness`; baseline `49dc752`.
- `46cf115`: experimental scene telemetry, saved model + train/export split, real broadband capture.
- `7ef7ad2`: four-stem + DS-1 selective compensation, observe fix, 3/4 dB ceilings, executor SHA and acknowledged worker bypass.
- `fc3cf74`: reject non-integer training labels.
- DS-1 refs and original edits preserved in `/Users/eranthe/Documents/Codex/2026-11-02/que/work/recovery/` (bundle, patch, 29-file archive). Refs preserved.
- DS-1 context/top2 self-music filter omitted. Both unvalidated model outputs excluded from dialogue memo keys; scene remains telemetry/display only.
- Four stems default: htdemucs, 2 s window/internal segment, 0.1 s overlap, autocast, strength 0.6; explicit six stems and `--no-stem-remix` supported.
- EQ off requests raw-sample worker bypass; queued audio/window/crossfade delay remains. Snapshot distinguishes request vs actual acknowledgement.

## Verification
- CTest 15/15 incl. export check. Initial incremental firmware build passed; post-commit reconfigure blocked by psutil process-list permission. Final rebuild unverified.
- systemeq build + selftest 11/11; launcher passed.
- Offline host: 102 registered groups / 8073 assertions passed; 9 permission-dependent groups excluded explicitly. Full host suite is NOT green/complete.
- Mock render 69/69; four-stem/fingerprint/bypass render 81/81.
- Role policy 6/6; worker components 3/3; scene/capture pipeline 2/2 (synthetic fixtures, not field evidence).
- User reports host restarted and separating. 16:16 startup log confirms serial source + Qwen config; live snapshot/browser blocked, actual stem/build still unverified. Did not kill/flash/restart.
- Escalation for local HTTP tests rejected because automatic approval model was unavailable; it was an approval-service failure, not a safety verdict. Local commits made; no push.

## Next
1. Restore local endpoint/process access; rerun full host suite and save live startup config before deployment.
2. Coordinate serial owner, flash matching artifact, restart through host_up with preserved parameters and existing cloud-audio authorization.
3. Follow `docs/acceptance-20261102.md`: 30 min playback, three-mode/leak A/B, 4 dialogue scenarios x3, cross-day voiceprint, S3 labelled capture. None completed this turn.
4. Old narrative archived in `handoff-archive-20261102.md`; no independent scene accuracy claim.
