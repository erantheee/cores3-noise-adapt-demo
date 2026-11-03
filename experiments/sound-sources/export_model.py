#!/usr/bin/env python3
"""Export a saved experimental model to staging; never train or establish field validation."""
import argparse
import json
import math
import re
from pathlib import Path

CLASSES = ["traffic", "horn_alarm", "speech", "wind", "music", "keyboard", "mechanical"]


def render(model):
    if (model.get("schema") != 1 or model.get("architecture") != [92, 24, 7]
            or model.get("classes") != CLASSES or model.get("feature_version") != "s3-context-92-v1"):
        raise ValueError("model schema, architecture, classes or features mismatch")
    if not isinstance(model.get("version"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", model["version"]):
        raise ValueError("version must be a short ASCII identifier")
    if model.get("field_validated") != [False]*7 or model.get("quiet_threshold") is not None:
        raise ValueError("this exporter cannot authorize field validation or a quiet threshold")
    flags = model.get("trained")
    if not isinstance(flags, list) or len(flags) != 7 or any(type(v) is not bool for v in flags) or not any(flags):
        raise ValueError("trained must identify at least one trained channel")
    arrays = [("kMean", model["normalization"]["mean"], 92),
              ("kScale", model["normalization"]["scale"], 92),
              ("kW1", model["w1"], 92*24), ("kB1", model["b1"], 24),
              ("kW2", model["w2"], 24*7), ("kB2", model["b2"], 7),
              ("kThresholds", model["thresholds"], 7)]
    lines = ["// Generated from a saved artifact. Experimental telemetry only.", "#pragma once",
             '#include "sound_sources.hpp"', "namespace sound_sources_model {"]
    for name, values, size in arrays:
        if (not isinstance(values, list) or len(values) != size
                or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 3e38 for v in values)):
            raise ValueError(f"invalid {name}")
        if name == "kScale" and any(v <= 0 for v in values):
            raise ValueError("scales must be positive")
        if name == "kThresholds" and any(not 0 < v < 1 for v in values):
            raise ValueError("thresholds must be inside (0,1)")
        literals = []
        for value in values:
            literal = format(float(value), ".9g")
            if "." not in literal and "e" not in literal:
                literal += ".0"
            literals.append(literal + "f")
        lines.append(f"constexpr float {name}[{size}] = {{" + ", ".join(literals) + "};")
    lines += ["constexpr bool kTrained[7] = {" + ", ".join(str(v).lower() for v in flags) + "};",
              "constexpr bool kValidated[7] = {false, false, false, false, false, false, false};",
              "inline const sound_sources::Model* get() {",
              "    static const sound_sources::Model model = {" + json.dumps(model["version"]) + ",",
              "        kMean, kScale, kW1, kB1, kW2, kB2, kThresholds, kTrained, kValidated};",
              "    return &model;", "}", "}  // namespace sound_sources_model", ""]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True, help="new staging header; explicit integration follows review")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    try:
        content = render(json.loads(args.model.read_text()))
        if args.check:
            if args.output.read_text() != content:
                raise ValueError("export differs from saved artifact")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as handle:
                handle.write(content)
    except (KeyError, ValueError, OSError) as exc:
        p.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
