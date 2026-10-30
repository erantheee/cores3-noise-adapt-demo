#!/usr/bin/env python3
"""Binary PCM worker for rolling six-stem separation.

stdin/stdout are raw little-endian stereo float32 PCM.  Diagnostics go only to
stderr so a Core Audio bridge can treat stdout as audio without framing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from demucs.api import Separator
from demucs.audio import convert_audio

from stem_remix import (
    STEMS,
    StemStats,
    contrast_only_profile,
    dominant_roles,
    remix_risk,
    role_profile,
)


def read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def stats_for(stems: dict[str, torch.Tensor]) -> dict[str, StemStats]:
    answer: dict[str, StemStats] = {}
    for name in STEMS:
        tensor = stems[name]
        rms = float(torch.sqrt(torch.mean(tensor.float().square())).item())
        peak = float(torch.max(torch.abs(tensor)).item())
        to_db = lambda value: 20.0 * np.log10(max(value, 1e-9))
        answer[name] = StemStats(float(to_db(rms)), float(to_db(peak)))
    return answer


def mix_stems(
    original: torch.Tensor,
    stems: dict[str, torch.Tensor],
    gains_db: dict[str, float],
) -> torch.Tensor:
    # Residual remix: retain the time/phase/spatial image of the captured mix,
    # and use estimated stems only for the requested delta.  Re-summing six
    # imperfect estimates as the whole signal produced a chorus/"3D" artifact
    # in the first QQ Music test.
    reference = next(iter(stems.values()))
    mixed = original.to(device=reference.device, dtype=reference.dtype).clone()
    for stem in STEMS:
        delta = (10.0 ** (gains_db[stem] / 20.0)) - 1.0
        mixed += stems[stem] * delta
    return mixed


def window_risk(
    original: torch.Tensor, stats: dict[str, StemStats], stems: dict[str, torch.Tensor]
) -> tuple[float, float, int, float]:
    rms = float(torch.sqrt(torch.mean(original.float().square())).item())
    peak = float(torch.max(torch.abs(original)).item())
    crest_db = 20.0 * np.log10(max(peak, 1e-9) / max(rms, 1e-9))
    strongest = max(value.rms_dbfs for value in stats.values())
    active = sum(
        value.rms_dbfs >= strongest - 18.0 and value.rms_dbfs > -50.0
        for value in stats.values()
    )
    vocals = stems["vocals"].float()
    if vocals.shape[0] >= 2:
        mid = (vocals[0] + vocals[1]) * 0.5
        side = (vocals[0] - vocals[1]) * 0.5
        mid_rms = float(torch.sqrt(torch.mean(mid.square())).item())
        side_rms = float(torch.sqrt(torch.mean(side.square())).item())
        side_ratio = side_rms / max(mid_rms, 1e-9)
    else:
        side_ratio = 0.0
    risk = remix_risk(float(crest_db), active, side_ratio)
    return risk, float(crest_db), active, side_ratio


class StreamingPeakLimiter:
    """Continuous zero-lookahead safety limiter whose state spans all windows."""

    def __init__(self, sample_rate: int, ceiling: float = 0.98, release_s: float = 0.8):
        self.ceiling = ceiling
        self.gain = 1.0
        self.release = float(np.exp(-1.0 / max(1.0, sample_rate * release_s)))

    def process(self, block: np.ndarray) -> np.ndarray:
        if len(block) == 0:
            return block
        output = np.empty_like(block)
        peaks = np.max(np.abs(block), axis=1)
        for index, peak in enumerate(peaks):
            target = min(1.0, self.ceiling / max(float(peak), 1e-9))
            if target < self.gain:
                self.gain = target
            else:
                self.gain = self.release * self.gain + (1.0 - self.release) * target
            output[index] = block[index] * self.gain
        return output


def emit_status(payload: dict) -> None:
    sys.stderr.write("STEMSTATUS " + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


VALID_ENVIRONMENTS = {"quiet", "broadband", "keyboard", "wind", "babble"}


def controlled_environment(path: str | None, fallback: str) -> str:
    if not path:
        return fallback
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")).get("environment")
    except (OSError, ValueError, AttributeError):
        return fallback
    return value if value in VALID_ENVIRONMENTS else fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--window", type=float, default=2.0)
    parser.add_argument("--overlap", type=float, default=0.5)
    parser.add_argument("--environment", default="broadband")
    parser.add_argument("--control-file")
    parser.add_argument("--model", default="htdemucs_6s")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--strength", type=float, default=0.6)
    args = parser.parse_args()

    if args.channels != 2:
        raise SystemExit("stream_worker currently requires stereo input")
    window_frames = round(args.sample_rate * args.window)
    overlap_frames = round(args.sample_rate * args.overlap)
    hop_frames = window_frames - overlap_frames
    if overlap_frames <= 0 or hop_frames <= 0:
        raise SystemExit("overlap must be greater than 0 and smaller than window")

    started = time.monotonic()
    separator = Separator(
        model=args.model,
        device=args.device,
        shifts=0,
        overlap=0.1,
        split=False,
        progress=False,
    )
    # Trigger MPS graph compilation before Core Audio starts feeding the first
    # audible window.  systemeq waits for this ready event before starting tap.
    warmup = torch.zeros((args.channels, window_frames), dtype=torch.float32)
    separator.separate_tensor(warmup, sr=args.sample_rate)
    emit_status(
        {
            "event": "ready",
            "model": args.model,
            "device": args.device,
            "load_seconds": round(time.monotonic() - started, 3),
            "window_seconds": args.window,
            "overlap_seconds": args.overlap,
            "strength": args.strength,
        }
    )

    input_buffer = np.empty((0, 2), dtype=np.float32)
    previous_tail: np.ndarray | None = None
    previous_roles: dict[str, str] | None = None
    window_index = 0
    limiter = StreamingPeakLimiter(args.sample_rate)
    while True:
        needed_frames = window_frames - len(input_buffer)
        raw = read_exact(sys.stdin.buffer, needed_frames * args.channels * 4)
        if not raw:
            break
        decoded = np.frombuffer(raw, dtype="<f4")
        complete = len(decoded) - (len(decoded) % args.channels)
        if complete == 0:
            break
        decoded = decoded[:complete].reshape(-1, args.channels)
        input_buffer = np.concatenate((input_buffer, decoded), axis=0)
        if len(input_buffer) < window_frames:
            break

        window = input_buffer[:window_frames]
        input_buffer = input_buffer[hop_frames:]
        tensor = torch.from_numpy(window.T.copy())
        infer_started = time.monotonic()
        original, stems = separator.separate_tensor(tensor, sr=args.sample_rate)
        stats = stats_for(stems)
        roles = dominant_roles(stats)
        if previous_roles is not None:
            # One-window hysteresis for streaming: a challenger must lead by
            # 2 dB before taking a role from the previous window.
            for role, owner in previous_roles.items():
                challenger = roles[role]
                if challenger != owner:
                    lead = stats[challenger].rms_dbfs - stats[owner].rms_dbfs
                    if lead < 2.0:
                        roles[role] = owner
        risk, crest_db, active_stems, vocal_side_ratio = window_risk(
            original, stats, stems
        )
        environment = controlled_environment(args.control_file, args.environment)
        requested_gains = role_profile(roles, environment)
        gains = contrast_only_profile(requested_gains, args.strength, risk)
        mixed = mix_stems(original, stems, gains)
        mixed = convert_audio(
            mixed, separator.samplerate, args.sample_rate, args.channels
        )
        output = mixed.detach().cpu().numpy().T.astype("<f4", copy=False)

        overlap_out = round(overlap_frames * len(output) / window_frames)
        hop_out = len(output) - overlap_out
        if previous_tail is None:
            emitted = output[:hop_out]
        else:
            fade = min(len(previous_tail), overlap_out)
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
            crossfade = previous_tail[:fade] * (1.0 - ramp) + output[:fade] * ramp
            emitted = np.concatenate((crossfade, output[fade:hop_out]), axis=0)
        emitted = limiter.process(emitted).astype("<f4", copy=False)
        sys.stdout.buffer.write(emitted.tobytes())
        sys.stdout.buffer.flush()
        previous_tail = output[hop_out:]
        previous_roles = roles
        elapsed = time.monotonic() - infer_started
        emit_status(
            {
                "event": "window",
                "index": window_index,
                "process_ms": round(elapsed * 1000.0, 1),
                "rtf": round(elapsed / args.window, 3),
                "roles": roles,
                "environment": environment,
                "gains_db": gains,
                "strength": args.strength,
                "risk": round(risk, 3),
                "crest_db": round(crest_db, 2),
                "active_stems": active_stems,
                "vocal_side_ratio": round(vocal_side_ratio, 3),
                "limiter_gain": round(limiter.gain, 4),
            }
        )
        window_index += 1

    if previous_tail is not None:
        tail = limiter.process(previous_tail).astype("<f4", copy=False)
        sys.stdout.buffer.write(tail.tobytes())
        sys.stdout.buffer.flush()
    emit_status({"event": "eof", "windows": window_index})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
