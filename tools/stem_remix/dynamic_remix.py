#!/usr/bin/env python3
"""Render a time-varying role-aware mix from six estimated stems."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf

from stem_remix import (
    ENVIRONMENTS,
    STEMS,
    RemixError,
    StemStats,
    locate_stems,
    require_program,
    role_profile,
    smooth_role_windows,
)


def dbfs(value: float) -> float:
    return float(20.0 * np.log10(max(value, 1e-9)))


def inspect_files(stems: dict[str, Path]) -> tuple[int, int, int]:
    metadata = [sf.info(stems[name]) for name in STEMS]
    first = metadata[0]
    for stem, info in zip(STEMS, metadata):
        if info.samplerate != first.samplerate or info.channels != first.channels:
            raise RemixError(f"Stem format mismatch: {stem}")
        if abs(info.frames - first.frames) > 2:
            raise RemixError(f"Stem duration mismatch: {stem}")
    return first.samplerate, first.channels, first.frames


def window_analysis(
    stems: dict[str, Path], sample_rate: int, window_seconds: float
) -> list[dict[str, StemStats]]:
    blocksize = max(1, round(sample_rate * window_seconds))
    handles = {stem: sf.SoundFile(path) for stem, path in stems.items()}
    result: list[dict[str, StemStats]] = []
    try:
        while True:
            blocks = {
                stem: handle.read(blocksize, dtype="float32", always_2d=True)
                for stem, handle in handles.items()
            }
            frames = max((len(block) for block in blocks.values()), default=0)
            if frames == 0:
                break
            stats: dict[str, StemStats] = {}
            for stem, block in blocks.items():
                rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
                peak = float(np.max(np.abs(block))) if len(block) else 0.0
                stats[stem] = StemStats(dbfs(rms), dbfs(peak))
            result.append(stats)
    finally:
        for handle in handles.values():
            handle.close()
    return result


def gain_curve(
    windows: list[dict[str, StemStats]], environment: str
) -> tuple[list[dict[str, str]], list[dict[str, float]]]:
    roles = smooth_role_windows(windows)
    gains = [role_profile(window_roles, environment) for window_roles in roles]
    return roles, gains


def render_float_mix(
    stems: dict[str, Path],
    output_wav: Path,
    gains: list[dict[str, float]],
    sample_rate: int,
    channels: int,
    window_seconds: float,
    transition_seconds: float,
) -> None:
    blocksize = max(1, round(sample_rate * window_seconds))
    transition_frames = max(1, round(sample_rate * transition_seconds))
    handles = {stem: sf.SoundFile(path) for stem, path in stems.items()}
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    previous = {stem: 0.0 for stem in STEMS}
    try:
        with sf.SoundFile(
            output_wav,
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            subtype="FLOAT",
        ) as writer:
            for target in gains:
                blocks = {
                    stem: handle.read(blocksize, dtype="float32", always_2d=True)
                    for stem, handle in handles.items()
                }
                frames = max((len(block) for block in blocks.values()), default=0)
                if frames == 0:
                    break
                mixed = np.zeros((frames, channels), dtype=np.float32)
                fade_frames = min(frames, transition_frames)
                for stem in STEMS:
                    block = blocks[stem]
                    if len(block) < frames:
                        block = np.pad(block, ((0, frames - len(block)), (0, 0)))
                    start_gain = 10.0 ** (previous[stem] / 20.0)
                    end_gain = 10.0 ** (target[stem] / 20.0)
                    envelope = np.full(frames, end_gain, dtype=np.float32)
                    envelope[:fade_frames] = np.linspace(
                        start_gain, end_gain, fade_frames, dtype=np.float32
                    )
                    mixed += block * envelope[:, None]
                writer.write(mixed)
                previous = target
    finally:
        for handle in handles.values():
            handle.close()


def encode_mp3(input_wav: Path, output_mp3: Path) -> None:
    ffmpeg = require_program("ffmpeg")
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_wav),
            "-af",
            "alimiter=limit=0.891,loudnorm=I=-14:TP=-1.0:LRA=11",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            str(output_mp3),
        ],
        check=False,
    )
    if completed.returncode:
        raise RemixError(f"ffmpeg failed to encode {output_mp3}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stems-dir", type=Path, required=True)
    parser.add_argument("--environment", choices=ENVIRONMENTS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--work-wav", type=Path, required=True)
    parser.add_argument("--window-seconds", type=float, default=2.0)
    parser.add_argument("--transition-seconds", type=float, default=0.25)
    args = parser.parse_args()

    try:
        stems = locate_stems(args.stems_dir)
        sample_rate, channels, frames = inspect_files(stems)
        windows = window_analysis(stems, sample_rate, args.window_seconds)
        roles, gains = gain_curve(windows, args.environment)
        render_float_mix(
            stems,
            args.work_wav,
            gains,
            sample_rate,
            channels,
            args.window_seconds,
            args.transition_seconds,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        encode_mp3(args.work_wav, args.output)

        role_counts = {
            role: dict(Counter(window[role] for window in roles))
            for role in ("lead", "rhythm", "low", "bed")
        }
        transitions = sum(
            roles[index] != roles[index - 1] for index in range(1, len(roles))
        )
        manifest = {
            "environment": args.environment,
            "window_seconds": args.window_seconds,
            "transition_seconds": args.transition_seconds,
            "duration_seconds": round(frames / sample_rate, 3),
            "window_count": len(windows),
            "role_change_windows": transitions,
            "role_counts": role_counts,
            "limitations": [
                "Environment is supplied by the CLI; live S3 wiring is not implemented yet.",
                "Role selection uses per-window stem energy, not melody recognition.",
                "Output is offline-rendered and does not prove real-time playback latency.",
            ],
        }
        manifest_path = args.manifest or args.output.with_suffix(".json")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
    except RemixError as exc:
        print(f"dynamic-remix: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
