from __future__ import annotations

import math

MAX_OFFSET = 86400 * 30


def check_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
    return value


def check_offset(offset_frames: object) -> int:
    return check_int("offset_frames", offset_frames, -MAX_OFFSET, MAX_OFFSET)


def check_timestamp(timestamp: object, allow_none: bool = False) -> float | None:
    if timestamp is None and allow_none:
        return None
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp < 0
    ):
        raise ValueError(f"timestamp must be a non-negative number of seconds, got {timestamp!r}")
    return float(timestamp)
