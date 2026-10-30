#!/usr/bin/env python3
"""Offline six-stem separation and role-aware remix demo.

This is intentionally separate from the real-time system-EQ path.  S3 still
owns the environment decision; this tool proves that the execution side can
protect musical roles instead of applying one EQ curve to the full mix.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")
ROLE_MAP = {
    "lead": ("vocals", "guitar", "piano", "other"),
    "rhythm": ("drums", "guitar", "piano"),
    "low": ("bass", "drums"),
    "bed": ("other", "guitar", "piano"),
}

# These are demo policies, not genre labels.  A rock track and an electronic
# track can both use rhythm_protect; a concerto and a folk track can both use
# lead_protect.  Values are deliberately modest because the final limiter and
# loudness match must not hide a large gain mistake.
PROFILES = {
    # Control condition: expose separation artifacts before judging a role
    # policy.  This is not the untouched source; it is the six estimated stems
    # recombined at 0 dB.
    "neutral_rebuild": {stem: 0.0 for stem in STEMS},
    "lead_protect": {
        "vocals": 3.0,
        "drums": -1.0,
        "bass": -1.0,
        "guitar": 1.5,
        "piano": 1.5,
        "other": 0.5,
    },
    "rhythm_protect": {
        "vocals": 0.5,
        "drums": 2.5,
        "bass": 1.5,
        "guitar": 0.5,
        "piano": -0.5,
        "other": -1.0,
    },
    "low_protect": {
        "vocals": 0.5,
        "drums": 1.0,
        "bass": 3.0,
        "guitar": -0.5,
        "piano": -0.5,
        "other": -1.0,
    },
}

ENVIRONMENTS = ("quiet", "broadband", "keyboard", "wind", "babble")


class RemixError(RuntimeError):
    pass


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class StemStats:
    rms_dbfs: float
    peak_dbfs: float


def require_program(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RemixError(f"Missing required program: {name}")
    return found


def run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise RemixError(f"Command failed ({completed.returncode}): {' '.join(command)}")


def locate_stems(root: Path) -> dict[str, Path]:
    found = {name: root / f"{name}.wav" for name in STEMS}
    missing = [str(path) for path in found.values() if not path.is_file()]
    if missing:
        raise RemixError("Missing stem files: " + ", ".join(missing))
    return found


def analyze_pcm(path: Path, sample_rate: int = 8_000) -> StemStats:
    """Measure a stem cheaply through ffmpeg without loading the WAV in RAM."""
    ffmpeg = require_program("ffmpeg")
    process = subprocess.Popen(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    if process.stdout is None:
        raise RemixError(f"Unable to read decoded samples from {path}")

    count = 0
    sum_squares = 0.0
    peak = 0.0
    while chunk := process.stdout.read(64 * 1024):
        usable = len(chunk) - (len(chunk) % 4)
        for (sample,) in struct.iter_unpack("<f", chunk[:usable]):
            absolute = abs(sample)
            peak = max(peak, absolute)
            sum_squares += sample * sample
            count += 1
    if process.wait() != 0:
        raise RemixError(f"ffmpeg failed while analyzing {path}")

    rms = math.sqrt(sum_squares / max(1, count))
    to_db = lambda value: 20.0 * math.log10(max(value, 1e-9))
    return StemStats(rms_dbfs=to_db(rms), peak_dbfs=to_db(peak))


def analyze_stems(stems: dict[str, Path]) -> dict[str, StemStats]:
    return {name: analyze_pcm(path) for name, path in stems.items()}


def dominant_roles(stats: dict[str, StemStats]) -> dict[str, str]:
    answer: dict[str, str] = {}
    for role, candidates in ROLE_MAP.items():
        answer[role] = max(candidates, key=lambda stem: stats[stem].rms_dbfs)
    return answer


def smooth_role_windows(
    windows: list[dict[str, StemStats]],
    margin_db: float = 2.0,
    hold_windows: int = 2,
) -> list[dict[str, str]]:
    """Select roles with hysteresis so small energy changes do not pump the mix."""
    if not windows:
        return []
    current = dominant_roles(windows[0])
    held = {role: 0 for role in ROLE_MAP}
    result = [dict(current)]
    for stats in windows[1:]:
        strongest = dominant_roles(stats)
        for role in ROLE_MAP:
            challenger = strongest[role]
            owner = current[role]
            if challenger == owner:
                held[role] += 1
                continue
            lead_db = stats[challenger].rms_dbfs - stats[owner].rms_dbfs
            if lead_db >= margin_db and held[role] >= hold_windows:
                current[role] = challenger
                held[role] = 0
            else:
                held[role] += 1
        result.append(dict(current))
    return result


def automatic_profile(stats: dict[str, StemStats]) -> dict[str, float]:
    """Create a conservative, genre-independent foreground policy.

    The strongest lead candidate is protected most.  The strongest rhythm and
    low-frequency candidates get smaller boosts.  Everything else is lowered
    slightly to create room.  This is a role heuristic, not source recognition.
    """
    roles = dominant_roles(stats)
    gains = {stem: -1.0 for stem in STEMS}
    gains[roles["lead"]] = 3.0
    gains[roles["rhythm"]] = max(gains[roles["rhythm"]], 1.5)
    gains[roles["low"]] = max(gains[roles["low"]], 1.0)
    return gains


def role_profile(roles: dict[str, str], environment: str) -> dict[str, float]:
    """Map time-local musical roles and an S3-style environment to stem gains."""
    if environment not in ENVIRONMENTS:
        raise RemixError(
            f"Unknown environment {environment!r}; expected one of {ENVIRONMENTS}"
        )
    if environment == "quiet":
        return {stem: 0.0 for stem in STEMS}

    gains = {stem: -1.0 for stem in STEMS}
    if environment == "broadband":
        targets = {"lead": 3.0, "rhythm": 1.5, "low": 1.0}
    elif environment == "keyboard":
        targets = {"lead": 3.0, "rhythm": 2.0, "low": 0.5}
    elif environment == "wind":
        # Do not spend scarce headroom boosting low-frequency content into wind.
        targets = {"lead": 2.5, "rhythm": 1.5, "low": -1.0}
    else:  # babble / competing voices
        targets = {"lead": 3.5, "rhythm": 1.0, "low": 0.0}

    for role, gain in targets.items():
        stem = roles[role]
        gains[stem] = max(gains[stem], gain)
    return gains


def contrast_only_profile(
    gains_db: dict[str, float], strength: float, risk: float = 0.0
) -> dict[str, float]:
    """Keep the top-priority stem at 0 dB and only attenuate competitors.

    Positive stem boosts amplified separation leakage and consumed headroom in
    the first live build.  Risk continuously reduces the contrast until the
    output becomes the untouched mix at risk=1.
    """
    strength = clamp(strength, 0.0, 1.0)
    risk = clamp(risk, 0.0, 1.0)
    top = max(gains_db.values(), default=0.0)
    scale = strength * (1.0 - risk)
    return {stem: min(0.0, (gains_db[stem] - top) * scale) for stem in STEMS}


def remix_risk(crest_db: float, active_stems: int, vocal_side_ratio: float) -> float:
    """Conservative risk gate for transients, dense mixes, and wide wet vocals."""
    transient = clamp((crest_db - 11.0) / 9.0, 0.0, 1.0)
    density = clamp((active_stems - 2.0) / 4.0, 0.0, 1.0)
    wet_width = clamp((vocal_side_ratio - 0.12) / 0.38, 0.0, 1.0)
    # A dry isolated drum should not force bypass (《渡口》反例); risk grows
    # fastest when transients and dense/reverberant content coexist.
    interaction = transient * max(density, wet_width)
    return clamp(0.55 * interaction + 0.30 * density + 0.15 * wet_width, 0.0, 1.0)


def render_mix(stems: dict[str, Path], gains: dict[str, float], output: Path) -> None:
    ffmpeg = require_program("ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for stem in STEMS:
        command.extend(["-i", str(stems[stem])])

    filters = []
    labels = []
    for index, stem in enumerate(STEMS):
        label = f"s{index}"
        filters.append(f"[{index}:a]volume={gains[stem]:.2f}dB[{label}]")
        labels.append(f"[{label}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(STEMS)}:normalize=0,"
        + "alimiter=limit=0.891,loudnorm=I=-14:TP=-1.0:LRA=11[out]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            str(output),
        ]
    )
    run(command)


def separate(input_path: Path, output_root: Path) -> Path:
    require_program("uvx")
    run(
        [
            "uvx",
            "--python",
            "3.11",
            "--from",
            "demucs",
            "--with",
            "numpy",
            "demucs",
            "-n",
            "htdemucs_6s",
            "--out",
            str(output_root),
            str(input_path),
        ]
    )
    return output_root / "htdemucs_6s" / input_path.stem


def write_manifest(
    path: Path,
    input_path: Path,
    stats: dict[str, StemStats],
    profiles: dict[str, dict[str, float]],
) -> None:
    payload = {
        "input": str(input_path),
        "model": "htdemucs_6s",
        "stems": {
            stem: {
                "rms_dbfs": round(value.rms_dbfs, 2),
                "peak_dbfs": round(value.peak_dbfs, 2),
            }
            for stem, value in stats.items()
        },
        "dominant_roles": dominant_roles(stats),
        "profiles_db": profiles,
        "limitations": [
            "The six stems are model estimates, not studio multitracks.",
            "neutral_rebuild is a zero-gain recombination, not the untouched source.",
            "The 'other' stem can contain strings, winds, synths, and effects.",
            "Dominant roles are energy heuristics and are not yet time-varying.",
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--stems-dir",
        type=Path,
        help="Reuse an existing directory containing the six WAV stems.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        stems_dir = args.stems_dir or separate(args.input, args.work_dir / "separated")
        stems = locate_stems(stems_dir)
        stats = analyze_stems(stems)
        profiles = {name: dict(gains) for name, gains in PROFILES.items()}
        profiles["auto_role_protect"] = automatic_profile(stats)

        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, gains in profiles.items():
            render_mix(stems, gains, args.output_dir / f"{args.input.stem}-{name}.mp3")
        write_manifest(
            args.output_dir / f"{args.input.stem}-stem-remix.json",
            args.input,
            stats,
            profiles,
        )
    except RemixError as exc:
        print(f"stem-remix: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
