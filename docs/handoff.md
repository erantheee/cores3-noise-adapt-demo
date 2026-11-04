# Session handoff — 2026-11-04

## 命名口径（2026-11-04，长期记忆）
- 核心能力统一叫「**包络调节**」（envelope modulation），不是「EQ」。EQ = 频率域静态曲线；包络调节 = 时间域动态轮廓（事件驱动）。见 `docs/decisions.md` D63、`docs/product-alignment.md` 命名口径段。

## 产品架构讨论（2026-11-04，未决，供下个会话继续）
- 起点：跨音源包络调节不一致（QQ 好，网易云/B站完全没用）。定位到 Mac 执行端 process tap 的 `mutedWhenTapped` 对网易云/B站失效：原声直出、没进处理链，tap 只抓到「没人听到的副本」（数据在动、听感为零）。与包络参数/频谱无关。
- 展开成产品级问题：全平台调音落点、分轨必要性、iOS 必 cover。
- 关键收敛（待用户拍板）：
  1. 端侧（耳机+仓）用低功耗 NPU 能闭环「感知 + 包络调节」，跑不动全尺寸分轨；录音戒指已证明小体积 NPU 可行。
  2. 产品是「端侧 + 上位机 + 云端」三层，不是纯端侧闭环：端侧 = 感知 + 包络调节快层，上位机 = 分轨（有音频流处）+ 决策，云端 = LLM 对话判断慢层。
  3. iOS 上全局音频接管无软件路径；要分轨全局只有「音频中继盒」：iPhone → A2DP → 仓（NPU 分轨 + 包络）→ 2.4G → 自研耳机。传输层「A2DP 进 + 2.4G 出」。
  4. 未决：分轨进不进端侧闭环、上位机形态（手机 app / 桌面 / 两端）、是否自研耳机。
- 本轮未改任何代码/固件；只新增 D63 + product-alignment 命名口径段。

## Global system-audio capture (2026-11-03, current chat)
- Implemented system target/default global stereo tap excluding own audio-process object; lookup failure aborts. Explicit bundle capture remains. Default launcher and panel/media labels updated; existing four-stem -> S3 envelope/EQ chain retained.
- Host restart preserved prior training-dir, voiceprint, voice gate, basic mode, ambient off and web port; no firmware flash. Previous binary and full snapshot/launch flags saved under this chat's work/global-audio.
- First global run fell back on starvation; follow-up starts with two output hops (3.8 s) instead of first burst. Final binary SHA 0fe67796be13f855d4c11a0c435665754823ce2786457d8eefec9942caefbbd2 verified live, scope=system/self_excluded=true/stems active. Initial final-run checks: zero underruns, increasing clipping; estimated pipeline frame lag ~4.6 s, not acoustic A/V measurement.
- Swift 25 selftests, host 111 groups/8152 assertions, launcher regression, deployed render 103 checks passed. See docs/acceptance-global-audio-20261103.md. No C++/firmware changes, so firmware rebuild not required.
- User reports source switching works, but reports weaker masking on NetEase Cloud Music versus QQ Music. No normalization/platform presets added; inspect equal-segment input levels, transient/headroom trim and actual gains first. Headroom trim persists across sources; 2 dB takes ~25 s under release conditions.
- Thirty-minute observation completed: 361/361 samples report global/four stems, max sampled RTF 0.579, zero sampled underruns. Two executor resets at ~810/~1770 s while host stayed up; engine rate changed 44.1 -> 48 -> 44.1 kHz, exact trigger unretained. Clipping grew. Uninterrupted/clipping acceptance NOT passed. User changed live mode to aggressive; preserve it. A/V timing, clipping and cross-source masking remain open.

## Git synchronization preparation (2026-11-03)
- User requested pushing the current version. Remaining mixed-source telemetry/pipeline/UI edits and prior dropout evidence are being committed with the existing ten local commits on feature/apple-conversation-awareness.
- Rechecked: CTest 18/18, host 111 groups/8136 assertions, synthetic pipeline 4/4, firmware build passed. No flashing or live-service restart.
- User reports current playback no longer stutters. Read-only snapshot during planning showed ordinary EQ active after stem underrun fallback; this does not establish that four-stem playback is continuously active. Masking benefit remains unaccepted.

## UI consolidation and acceptance refresh (19:55)
- User rejected standalone sound-sources card; merged its existing IDs into ctx-card as one subline. Valid-source details are collapsed; unavailable data has no expanded detail area. Existing concurrent sound-source implementation preserved. render_sound_sources.js passed; HTML nesting check passed. Server reads HTML per request; browser refresh suffices.
- 19:53 EQ unavailable; by 19:55 host uptime reset (not restarted by this check) and candidate SHA151d9a9aaa78… EQ applied. 8-second follow-up: zero underrun, clips310→342, stems disabled after starvation. Voiceprint ready. Overall audio acceptance NOT passed; four-stem stability and listening tests remain open.

## Mixed-source classification implementation (separate chat, 2026-11-02)
- Manual approval retry completed (20:08): firmware build passed; binary SHA256 `5bba38aec038607548900e54fe2ffcc8fa5bae80214d7023e7139c05870e6feb`. Full host tests passed: 111 groups / 8136 assertions (`/tmp/sound-sources-host-manual.log`). Final CTest 18/18 and pipeline 4/4 passed. No deployment or shared-service restart.
- Working checkout `feature/apple-conversation-awareness`, observed HEAD `be6ae74`; edits uncommitted. Preserved other chats' handoff, dropout evidence and deployment work. This chat did not flash, restart the host, take the serial port, play media or upload audio.
- Added display-only `sound_sources` telemetry: seven independent sigmoid channels, portable 92→24→7 inference, continuity/input-invalid states, compact score/mask wire format, validated host parsing and panel clearing on stale/disconnected data. Legacy event/controller/EQ/dialogue/memo inputs unchanged by this implementation.
- Added `collect_teacher_data.py --window-vectors` (new directories only), exact S3 feature-version/cadence markers; overlapping human annotation template/build/audit; masked multi-label NumPy training; train-only normalization/fit and validation-only thresholds; source/session split rejection; immutable saved artifacts/staging export. Pipeline and class/acceptance contract: `experiments/sound-sources/README.md`.
- No real seven-label model trained/installed: inspected four tracked and ten local session directories (some copies); no new exact-vector records or multi-label human annotations. Two local voiceprint sessions report audio gaps. Default model accessor remains unavailable; quiet calibration remains null; all classes unvalidated. Existing model top2 is not coexisting-source evidence.
- Verification: CTest 18/18 (C++, protocol/collector and Node render tests); pipeline 4/4 synthetic tests, including Python/C++ exported-score agreement and test-label isolation. Python syntax and whitespace checks passed. Synthetic results do not establish S3 accuracy, runtime/bandwidth, or field benefit.
- Earlier restricted full host run had 9 permission-related failures and firmware configuration was blocked by sysctl access; manual approval resolved both as recorded above. Earlier deployment results below do NOT validate these new source edits on hardware.
- Next: coordinate live serial owner before any deployment/capture. Collect real single/mixed conditions, manually review overlapping labels, then train/evaluate by independent source/session. Require real-S3 per-class all+mixed precision/recall ≥80%, FPR ≤10%, ≥60 s positive and negative coverage and ≥3 test sessions per subset, then device added inference ≤2 ms/frame/no extra losses. New outputs stay telemetry only.

## Deployment and underrun fix (this thread, latest)
- Commits c44f722 (underrun counters/fallback, bounded resampling demand, hop-based RTF), be6ae74 (macOS detached host uses new process session). Swift selftest 15/15; host 111 groups/8136 assertions; launcher passed.
- Flashed firmware SHA 7918aa783d1aa7202dbd3465c1299fd6a09671c27cf36145efd54b0ce8b562bd. Fresh serial scene_valid telemetry confirmed. Executor SHA 151d9a9aaa78e8d67c5e82e820aab368daa18863ed3c16e5e7bd71d7f253bb16 confirmed live.
- Deployment owner was DS-1 PID26462; preserved QQMusic/basic/voice gate/ambient off and DS-1 training/memo dirs. Corrected voiceprint enrollment to main checkout 记录/声纹/self-device (3 existing WAVs); worker now ready, no error.
- Four-stem startup actually triggered new underrun fallback. Ordinary EQ then showed 0 underrun frames in an 8 s check, but +5 clipped samples. This is fallback validation, NOT stable stem/listening acceptance.
- Latest later snapshot: host alive >2400 s, voiceprint ready; EQ unavailable after repeated output-device configuration changes triggered >3 restarts/60 s protection. Do not claim healthy audio. Investigate route churn before restarting again.
- Concurrent sound_sources edits appeared in main/host/UI; preserved, not included in these commits. Flash log and live JSONs in 2026-11-02/que/work/deploy and outputs. No push yet.

## Listening feedback: masking not achieved (read-only diagnosis)
- User reports smoother sound after stems, but no audible masking. Treat comfort improvement and masking acceptance separately; masking remains failed by user listening feedback.
- Checkout observed at `c44f722`; existing handoff/launcher edits preserved. Live snapshots: stem_requested=true but stem_stream=false, stem_ready=false, fallback_reason=分轨输出欠载，退出并回退普通 EQ. No restart or parameter changes.
- Swift masking compares independently normalized environment/song spectral shapes, not calibrated at-ear music/noise SPL. Content-presence gate suppresses sparse song bands; transient gate, positive gain ceilings and headroom further constrain boosts. Latest snapshot masking_peak_db≈1.54, headroom_trim_db≈1.97; these alone do not quantify net masking or prove the cause of this listening result.
- Next: identify noise type and listening conditions; distinguish desired reduction in noise noticeability from full coverage. Validate controlled listening A/B before claiming masking. No code/test/deployment changes in this check.

## Approval diagnosis (latest)
- 18:53 user applied configure.py successfully (backup config.toml.before-auto-review-20261102-185338-160925). Current config parsed: on-request + auto_review; session permission instructions also now enable auto_review. Actual escalated read-only `sub2api-client mode status --json --non-interactive` still rejected before execution: `Model "codex-auto-review" is not available for this group`. Configuration repair applied, automatic approval NOT restored. Previous manual/granular mode no longer explains this latest failure; next investigation must trace reviewer request/model support along proxy/upstream route, without assuming which layer owns the error.
- Latest log 10:50:20 UTC for this chat confirms application-supplied `requestApprovalsReviewer=user`, permission profile `:workspace`, granular sandbox/rules/skill approvals=false. User explicitly wants automatic review. Root config still omits both approval keys; changing root defaults alone does not demonstrate removal of this chat override.
- Prepared `/Users/eranthe/Documents/Codex/2026-11-02/auto-approval/configure.py`: preview or --apply, backups, parsed semantic check limiting changes to on-request + auto_review, preserves sandbox/provider, detects concurrent edits. Preview validated against current config; no global write performed (outside writable roots; sandbox approvals disabled). User must apply and select automatic approval in application, then verify real eligible approval. No repair-success claim.
- Correction after user consulted pool operator: model-unavailable response establishes failure of the automatic-review request, not proof of a pool-side permission restriction. User's live doctor confirms agent ready, key/subscription/access valid and pool reachable.
- Current root config has neither approval_policy nor approvals_reviewer. User screenshot proposes on-request + auto_review; official config reference says auto_review invokes reviewer subagent, so that alone does not establish a fix for missing reviewer model.
- Current chat runtime now injects granular policy with sandbox_approval/rules/skill_approval=false; these categories automatically reject. Root-file defaults do not prove a change to this existing chat. No permission settings changed in this turn.
- Local listener `127.0.0.1:58127` exists: PID 2924, Sub2API agent 0.6.2. Config provider `sub2api-lan`, model `gpt-6.1-sol`, local backend-api/codex URL.
- Read-only escalated `sub2api-client doctor --all --live --json --non-interactive` was rejected before execution: `Automatic approval review failed: Model "codex-auto-review" is not available for this group`. Explicitly a review-service failure, not an unsafe-action determination.
- Sandbox-only doctor/status cannot access agent reliably; their stopped/key-absent fields do not prove actual service/credential state. No provider/permission/agent changes made. User-reported 503 request `0b7f4144-3e00-41e0-a44a-896b49800550` remains untraced.
- User Terminal run rejected `doctor --all --live --json --non-interactive` with BAD_ARGUMENT. Verified installed protocol.sh: JSON doctor accepts only optional --all, invokes route refresh. Correct command: `sub2api-client doctor --all --json --non-interactive` (no --live). Earlier description as purely read-only was inaccurate: JSON doctor refreshes the route.
- Next: user runs corrected doctor in Terminal; gateway operator checks group availability/routing for approval model and traces the 503 request. Do not bypass approval checks.

## Music dropout diagnosis (later read-only check)
- Checkout HEAD `d438ea4`; running binary SHA `d68567cb0eac1f67ffb45942c092be907db3645a854b0188aba5b935ccfe406f`, verified against DS-1 worktree binary. No restart/config/firmware change.
- Live four-stem htdemucs 2 s windows, 0.1 s overlap => 1.9 s output/hop. Observed process_ms 2312.1 and 2128.7: windows can miss playback deadlines. No stem error/fallback; media_percent stayed 100 throughout 20 samples.
- Evidence: `docs/music-dropout-20261102.json`. Over sample endpoints, output meter advanced 1772820 stereo samples (=886410 frames), source supplied 878350 frames: ~8060 frames/183 ms deficit at 44.1 kHz (reporting skew applies). Confirms output supply gaps; source code fills shortage with zeros. No listening A/B performed.
- Running source uses stem-only playback ring, initial >=24000-frame preroll, no immediate dry fallback on starvation; watchdog only detects high backlog. RTF divides by 2 s window rather than 1.9 s hop. Resampler also retains unfilled input demand after starvation; assess in fix.
- CPU/process inspection blocked by broken automatic approval reviewer (`codex-auto-review` unavailable); resource contention/thermal cause of latency spikes remains unverified. Next: preserve live startup flags, fix underrun handling and measure bypass/stem A/B before claiming listening resolution.

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
- CTest 15/15 incl. export check. Final firmware rebuild passed after manual approval; SHA 7918aa783d1aa7202dbd3465c1299fd6a09671c27cf36145efd54b0ce8b562bd.
- systemeq build + selftest 11/11; launcher passed.
- Full host suite now passed: 111 groups / 8133 assertions after manual approval (work/host-tests-unrestricted.log in chat workspace).
- Mock render 69/69; four-stem/fingerprint/bypass render 81/81.
- Role policy 6/6; worker components 3/3; scene/capture pipeline 2/2 (synthetic fixtures, not field evidence).
- Read user-saved snapshots 17:03/17:07: serial live, htdemucs four stems ready, +4 windows/8 s, RTF 0.330–0.395, no stem error/fallback. Running SHA d68567cb0eac differs from disk candidate 948f33d1c174; requested/bypass fields missing. Clips +5/+2 per 8 s, +1161 across ~250 s. Sparse snapshots, not 30-min/listening acceptance. Did not kill/flash/restart.
- Approval root evidence: latest request to localhost:58127/backend-api/codex/responses returned 404 for codex-auto-review. Config now sub2api-lan; Sub2API agent intent pool (revision 16). Earlier config-only repair did not restore access. Managed sub2api-client mode direct syncs intent/config but its execution was also rejected by broken approval. Resolved later by selecting manual approval, retaining Sub2API; live read, process inspection, full tests and build succeeded. Local commits; no push.

## Next
1. Access restored via manual approval; 17:58 live snapshots +4 stem windows/8 s, clips +25. Owner PID 26462 runs DS-1 copy at 2026-10-30/feature-apple-conversation-awareness-ds-1/work/cores3-ds/tools. Preserve voiceprint, voice gate, stem, QQMusic basic, ambient off, training-dir from that copy. Do not deploy over shared live work without coordination.
2. Coordinate serial owner, flash matching artifact, restart through host_up with preserved parameters and existing cloud-audio authorization.
3. Follow `docs/acceptance-20261102.md`: 30 min playback, three-mode/leak A/B, 4 dialogue scenarios x3, cross-day voiceprint, S3 labelled capture. None completed this turn.
4. Old narrative archived in `handoff-archive-20261102.md`; no independent scene accuracy claim.
