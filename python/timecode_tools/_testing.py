"""Test-signal helpers. Not part of the public API."""

from __future__ import annotations

import array

from . import _native
from ._timecode import FrameRate, FrameRateLike, Timecode, _frame_rate


def generate_ltc(
    sample_rate: int = 48000,
    frame_rate: FrameRateLike = FrameRate.FPS_25,
    start: str = "00:00:00:00",
    frame_count: int = 25,
    amplitude: float = 0.6,
) -> array.array[int]:
    """Mono LTC as ``array.array('h')``."""
    rate = _frame_rate(frame_rate, False)
    index = Timecode.parse(start, rate).frame_index()
    return array.array("h", _native.generate_ltc(sample_rate, rate.value, index, frame_count, amplitude))


def generate_mtc(
    frame_rate: FrameRateLike = FrameRate.FPS_25, start: str = "00:00:00:00", frame_count: int = 50
) -> list[tuple[bytes, float]]:
    """Quarter-frame messages with their send times in seconds, from 0."""
    rate = _frame_rate(frame_rate, False)
    data, times = _native.generate_mtc(rate.value, Timecode.parse(start, rate).frame_index(), frame_count)
    return [(data[i * 2 : i * 2 + 2], t) for i, t in enumerate(times)]


def mtc_full_frame(timecode: str, frame_rate: FrameRateLike) -> bytes:
    rate = _frame_rate(frame_rate, False)
    tc = Timecode.parse(timecode, rate)
    return _native.mtc_full_frame(rate.value, (tc.hours, tc.minutes, tc.seconds, tc.frames, tc.drop_frame))
