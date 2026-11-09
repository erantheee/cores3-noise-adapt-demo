#!/usr/bin/env python3
"""Bridge the legacy systemeq stem-worker pipe to the Edge2 PCM service."""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from pathlib import Path

import numpy as np

HEADER = struct.Struct("<IIQIHHi")
MAGIC = 0x4B4F5753
EDGE_RATE = 44_100


def emit(payload: dict) -> None:
    sys.stderr.write("STEMSTATUS " + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def read_exact(handle, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = handle.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("Edge2 closed the PCM connection")
        chunks.extend(chunk)
    return bytes(chunks)


def resample(block: np.ndarray, frames: int) -> np.ndarray:
    if len(block) == frames:
        return block.astype(np.float32, copy=False)
    old = np.linspace(0.0, 1.0, len(block), endpoint=False)
    new = np.linspace(0.0, 1.0, frames, endpoint=False)
    return np.stack([np.interp(new, old, block[:, channel]) for channel in range(2)], axis=1).astype(np.float32)


def control(path: str | None, fallback: str) -> tuple[str, bool]:
    if not path:
        return fallback, False
    try:
        value = json.loads(Path(path).read_text())
        return str(value.get("environment") or fallback), value.get("bypass") is True
    except (OSError, ValueError, TypeError):
        return fallback, False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-rate", type=int, required=True)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--window", type=float, default=4.0)
    parser.add_argument("--overlap", type=float, default=0.1)
    parser.add_argument("--segment")
    parser.add_argument("--environment", default="broadband")
    parser.add_argument("--control-file")
    parser.add_argument("--model", default="edge2-rknn")
    parser.add_argument("--device", required=True, help="Edge2 base URL")
    parser.add_argument("--strength", type=float, default=0.6)
    parser.add_argument("--autocast", action="store_true")
    args = parser.parse_args()
    if args.channels != 2:
        raise SystemExit("Edge2 bridge requires stereo")

    host = args.device.removeprefix("http://").removeprefix("https://").split("/", 1)[0].split(":", 1)[0]
    window_frames = round(args.sample_rate * args.window)
    overlap_frames = round(args.sample_rate * args.overlap)
    hop_frames = window_frames - overlap_frames
    if hop_frames <= 0:
        raise SystemExit("overlap must be smaller than window")
    edge_frames = round(EDGE_RATE * args.window)
    sequence = 0
    buffered = np.empty((0, 2), dtype=np.float32)
    previous_tail: np.ndarray | None = None
    emit({"event": "ready", "model": "edge2-rknn", "device": host,
          "window_seconds": args.window, "overlap_seconds": args.overlap,
          "segment_seconds": args.window, "autocast": False})

    sock = socket.create_connection((host, 9100), timeout=5.0)
    sock.settimeout(15.0)
    try:
        while True:
            needed = window_frames - len(buffered)
            raw = read_exact(sys.stdin.buffer, needed * 2 * 4)
            if not raw:
                break
            decoded = np.frombuffer(raw, dtype="<f4")
            decoded = decoded[: len(decoded) - (len(decoded) % 2)].reshape(-1, 2)
            buffered = np.concatenate((buffered, decoded), axis=0)
            if len(buffered) < window_frames:
                break
            window = buffered[:window_frames]
            buffered = buffered[hop_frames:]
            environment, bypass = control(args.control_file, args.environment)
            started = time.monotonic()
            if bypass:
                output = window.copy()
            else:
                edge_input = resample(window, edge_frames)
                header = HEADER.pack(MAGIC, 1, sequence, EDGE_RATE, 2, 0, edge_frames)
                sock.sendall(header + edge_input.astype("<f4", copy=False).tobytes())
                response = recv_exact(sock, HEADER.size)
                magic, schema, returned_sequence, rate, channels, _reserved, returned_frames = HEADER.unpack(response)
                if (magic, schema, returned_sequence, rate, channels) != (MAGIC, 1, sequence, EDGE_RATE, 2):
                    raise RuntimeError("invalid Edge2 PCM response header")
                edge_output = np.frombuffer(recv_exact(sock, returned_frames * 2 * 4), dtype="<f4").reshape(-1, 2)
                if not np.isfinite(edge_output).all():
                    raise RuntimeError("non-finite Edge2 output")
                output = resample(edge_output, window_frames)

            if previous_tail is None:
                emitted = output[:hop_frames]
            else:
                fade = min(len(previous_tail), overlap_frames)
                ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
                crossfade = previous_tail[:fade] * (1.0 - ramp) + output[:fade] * ramp
                emitted = np.concatenate((crossfade, output[fade:hop_frames]), axis=0)
            previous_tail = output[hop_frames:]
            sys.stdout.buffer.write(emitted.astype("<f4", copy=False).tobytes())
            sys.stdout.buffer.flush()
            elapsed = time.monotonic() - started
            emit({"event": "window", "bypass": bypass, "index": sequence,
                  "process_ms": round(elapsed * 1000.0, 1),
                  "rtf": round(elapsed / (hop_frames / args.sample_rate), 3),
                  "environment": environment, "stems": ["vocals", "drums", "bass", "other"],
                  "roles": {}, "gains_db": {}, "strength": args.strength})
            sequence += 1
    finally:
        sock.close()
    if previous_tail is not None:
        sys.stdout.buffer.write(previous_tail.astype("<f4", copy=False).tobytes())
        sys.stdout.buffer.flush()
    emit({"event": "eof", "windows": sequence})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
