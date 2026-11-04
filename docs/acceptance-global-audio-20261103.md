# System audio capture candidate — 2026-11-03

## Implemented behavior

`sh tools/host_up.sh --detach` now defaults to `--system-eq-app system` and the existing four-stem htdemucs chain. Core Audio uses a stereo global tap excluding the executor audio-process object. Failed self-process resolution aborts capture; no empty exclusion fallback exists. Explicit `--system-eq-app com.tencent.QQMusicMac` and `--no-stem-remix` remain supported.

All included apps mix together; source selection is not based on foreground windows. Browser audio subprocesses and newly started apps are covered by the global tap. Protected content remains unverified. This does not identify websites, video titles or songs. The existing voiceprint/ASR pipeline still receives the S3 microphone, not the global tap PCM.

Four-stem remixes still feed S3 event envelopes, the 24-band envelope modulation and the existing masking matrix. Actual/requested stem states and fallback reasons remain separate. System audio capture does not prove masking benefit.

## Continuity and latency

The tap stays alive across apps, tabs and playback pauses; global silence can continue through the worker as zero PCM. No source-change listener tears down or flushes the chain. Existing queued audio retains the pipeline delay; source changes cannot be claimed instantaneous. Recent content-window RMS replaces lifetime RMS for current playback/silent status.

An initial deployed build reverted to ordinary envelope modulation after stem underrun. A follow-up uses two output hops of preroll (3.8 s at 2 s/0.1 s window/overlap) rather than starting with the first batch. The capacity is unchanged. This absorbs temporary processing spikes, not sustained RTF >=1. Excess backlog/underrun still triggers ordinary-envelope-modulation fallback. An 8 s preroll wait limit remains, so a timeout may start with less audio; watch the underrun counters during acceptance.

At the first follow-up sample, preroll wait was 3760 ms and input-vs-consumed frame lag approximately 4591 ms. Frame lag is an estimate from asynchronous counters, excluding output-driver/acoustic delay and not a synchronized A/V measurement. User acceptance of video delay remains open.

## Verification and limitations

- Swift build/selftest: 25 checks passed; covers global exclusion, single-app selection, missing app, self-lookup errors and rate-dependent preroll.
- Host tests: 111 groups / 8152 assertions passed; includes global media labels, recent-RMS pause/resume and buffer telemetry.
- Launcher regression passed: defaults to system/four stems; explicit single-app/no-stem settings preserved.
- Deployed-snapshot render: 103 passed, zero failed. Fixed old misleading "24 bands zeroed" stem wording.
- User reported switching sources works (2026-11-03). No separate claim for multi-tab, concurrent playback, protected content, permission denial or acoustic A/V timing.
- Final build deployed with saved training directory, voiceprint, voice gate, basic mode and ambient executor off. S3 firmware unchanged. Automated sustained-run results will be recorded separately after completion.
- Early final-build snapshots showed zero underruns but increasing post-envelope-modulation clipping counters. Listening quality and clipping acceptance remain open.

## Recovery

Use the saved startup arguments and previous executor under this chat's `work/global-audio/` for exact rollback, after inspecting the current serial owner. For a single-app capture trial keep the new binary and launch through host_up with `--system-eq-app com.tencent.QQMusicMac`; retain the other saved feature flags. For no stems pass `--no-stem-remix` explicitly. Stop through `/api/stop`, then use free_port and host_up; never leave the serial owner stopped without replacement.

## Additional user feedback during the run

The user reports different masking strength between QQ Music, NetEase Cloud Music and Bilibili, including insufficient masking on NetEase with parameters that work on QQ Music. No controlled same-master/same-segment/equal-level A/B has established the cause. Do not mark cross-platform masking consistency accepted.

Code inspection confirms that the current matrix compares independently normalized spectral shapes; it does not calibrate at-ear music/noise level. Transient gating and post-envelope-modulation clipping protection also change actual positive gains. The clipping trim is retained across source changes and releases by 0.004 dB per 50 ms only when the measured peak is below 0.90: 2 dB needs about 25 s under continuous release conditions. Do not reset this protection blindly on source switches or add per-platform boost presets from this observation.

During monitoring, the user changed the live mode from basic to aggressive (dose 1.4); startup default remains basic. Preserve the user's live selection. One stem-window counter reset occurred at observation ~810 s while host uptime continued. Subsequent output engine rate was 48 kHz versus the earlier 44.1 kHz; the exact triggering app and quitting reason were not retained. Continuous-uninterrupted playback acceptance therefore cannot be inferred from zero sampled underruns. Later asynchronous frame-count lag was about 4.73 s, excluding output-driver/acoustic delay.

## Completed 30-minute sampled observation

Duration 1800.01 s, 361 samples every 5 s. All samples report system scope, self exclusion, available execution and four stems; 348 samples have recent input RMS >0.0005. Maximum sampled RTF 0.579; maximum input ring 35840 frames; output ring 187456 frames. No sampled underrun counts above zero.

Two executor resets were detected at 810.0 s and 1775.01 s, with engine-rate transitions 44100 -> 48000 and 48000 -> 44100. Host uptime continued. Reset reason was not retained, so this is not proof of the responsible app. Post-envelope-modulation clipping counters increased and reset with executors. This run does not pass uninterrupted/clipping/listening acceptance; sample availability must not be reported as seamless audio quality.
