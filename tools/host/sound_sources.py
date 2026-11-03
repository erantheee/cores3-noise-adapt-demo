"""Validate display-only multi-source telemetry independently of control fields."""
import math

CLASSES = ("traffic", "horn_alarm", "speech", "wind", "music", "keyboard", "mechanical")
LABELS = ("车流", "鸣笛／警报", "人声", "风噪", "音乐", "键盘", "稳态机械声")
STATUSES = {"ready", "window_incomplete", "input_invalid", "model_unavailable", "model_invalid"}


def parse_sources(raw):
    if (not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != 1
            or not isinstance(raw.get("status"), str) or raw["status"] not in STATUSES):
        raise ValueError("invalid sound_sources status")
    status = raw["status"]
    if type(raw.get("valid")) is not bool or raw["valid"] != (status == "ready"):
        raise ValueError("inconsistent sound_sources validity")
    if not isinstance(raw.get("version"), str) or not raw["version"].strip():
        raise ValueError("missing sound_sources version")
    if type(raw.get("inference_us")) is not int or raw["inference_us"] < 0:
        raise ValueError("invalid sound_sources inference time")
    if raw.get("quiet") is not None:
        raise ValueError("quiet has no calibrated threshold yet")
    scores = raw.get("scores")
    if not isinstance(scores, list) or len(scores) != len(CLASSES):
        raise ValueError("sound_sources must report all seven scores")
    if raw["valid"] and all(v is None for v in scores):
        raise ValueError("ready telemetry needs at least one available channel")
    detected_mask, validated_mask = raw.get("detected_mask"), raw.get("validated_mask")
    if any(type(v) is not int or not 0 <= v <= 127 for v in (detected_mask, validated_mask)):
        raise ValueError("invalid sound_sources bit masks")
    clean = {}
    for c, name in enumerate(CLASSES):
        score = scores[c]
        detected, validated = bool(detected_mask & (1 << c)), bool(validated_mask & (1 << c))
        if score is None:
            if detected or validated:
                raise ValueError("unavailable channel cannot report a detection/validation")
        elif (type(score) not in (int, float) or not math.isfinite(score)
              or not 0 <= score <= 1 or not raw["valid"]):
            raise ValueError("invalid sound_sources score/detection")
        clean[name] = dict(score=score, detected=detected if score is not None else None, validated=validated)
    return dict(status=status, valid=raw["valid"], version=raw["version"],
                inference_us=raw["inference_us"], quiet=None, channels=clean)


def snapshot_sources(raw, live):
    if raw is None:
        return None
    result = dict(raw)
    if not live:
        result.update(status="data_interrupted", valid=False, quiet=None,
                      channels={name: dict(score=None, detected=None, validated=False)
                                for name in CLASSES})
    return result
