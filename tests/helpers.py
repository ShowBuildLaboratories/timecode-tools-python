"""Shared test helpers."""

from __future__ import annotations

import array
from collections.abc import Iterator

from timecode_tools import PcmChunk, Timecode

RATE = 48000


def concat(*parts: array.array) -> array.array:
    out = array.array("h")
    for p in parts:
        out.extend(p)
    return out


def silence(n: int) -> array.array:
    return array.array("h", bytes(2 * n))


def chunks(pcm: array.array, block: int = 480, t0: float = 1.0, channels: int = 1) -> Iterator[PcmChunk]:
    """Splits PCM into blocks (10 ms by default) with timestamps from a fake
    monotonic clock."""
    for i in range(0, len(pcm), block):
        yield PcmChunk(pcm[i : i + block], RATE, channels, t0 + i / channels / RATE)


def record(rx) -> list[str]:
    log: list[str] = []
    rx.on("lock", lambda info: log.append(f"lock {info.timecode} {info.frame_rate}"))
    rx.on("jump", lambda tc: log.append(f"jump {tc}"))
    rx.on("lost", lambda: log.append("lost"))
    return log


# The reference LTC test encoder the golden files were made with: a square
# wave (no rise time, no noise) at the nominal frame rate, amplitude 20000,
# eight preamble cells of 1 bits before the frames and eight 0 bits after.

SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]


def frame_bits(tc: Timecode) -> list[int]:
    bits = [0] * 80
    fields = [
        (0, 4, tc.frames % 10),
        (8, 2, tc.frames // 10),
        (16, 4, tc.seconds % 10),
        (24, 3, tc.seconds // 10),
        (32, 4, tc.minutes % 10),
        (40, 3, tc.minutes // 10),
        (48, 4, tc.hours % 10),
        (56, 2, tc.hours // 10),
    ]
    for start, count, value in fields:
        for k in range(count):
            bits[start + k] = (value >> k) & 1
    bits[10] = 1 if tc.drop_frame else 0
    bits[64:80] = SYNC
    return bits


def js_round(x: float) -> int:
    # Math.round: halves round up (Python's round() rounds them to even).
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def reference_ltc(
    sample_rate: int, frame_rate: str, nominal_fps: int, start: str, frame_count: int, amplitude: int = 20000
) -> array.array:
    cell = sample_rate / (nominal_fps * 80)
    levels = []  # (time in samples, level)
    target = 0.0
    level = 1

    def cell_out(bit: int) -> None:
        nonlocal target, level
        level = -level
        levels.append((target, level))
        if bit == 1:
            target += cell / 2
            level = -level
            levels.append((target, level))
            target += cell / 2
        else:
            target += cell

    for _ in range(8):
        cell_out(1)
    index = Timecode.parse(start, frame_rate).frame_index()
    for n in range(frame_count):
        for bit in frame_bits(Timecode.from_frame_index(index + n, frame_rate)):
            cell_out(bit)
    for _ in range(8):
        cell_out(0)
    length = js_round(target)
    pcm = array.array("h", bytes(2 * length))
    edge = 0
    for s in range(length):
        while edge + 1 < len(levels) and js_round(levels[edge + 1][0]) <= s:
            edge += 1
        pcm[s] = js_round(levels[edge][1] * amplitude)
    return pcm
