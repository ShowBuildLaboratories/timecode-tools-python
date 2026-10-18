from __future__ import annotations

import array
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# A buffer of samples: a numpy array, array.array, memoryview, or bytes.
Samples = Any
MidiBytes = bytes | bytearray | memoryview | Iterable[int]


@dataclass
class PcmChunk:
    """A block of PCM audio for :class:`LtcReceiver`.

    ``samples`` is interleaved audio: any buffer of int16 or float32 values
    (floats in -1..1), such as a numpy array (1-D, or 2-D ``(frames,
    channels)`` as audio libraries deliver it), an ``array.array('h')`` or
    ``array.array('f')``. float64 is converted. ``bytes`` and ``bytearray``
    are read as 16-bit little-endian samples (``ffmpeg -f s16le``).

    ``channels`` defaults to the second dimension of a 2-D array, else 1.

    ``timestamp`` is when the chunk's first sample was captured, in seconds
    on a monotonic clock: ``time.perf_counter()``, or the capture time your
    audio library reports. If it is ``None``, the time the chunk is received
    (``time.perf_counter()``) is used instead, which is less precise.
    """

    samples: Samples
    sample_rate: int
    channels: int | None = None
    timestamp: float | None = None


@dataclass
class MidiChunk:
    """Raw MIDI bytes for :class:`MtcReceiver`, with the time in seconds
    they arrived on a monotonic clock such as ``time.perf_counter()``
    (``None``: the time they are received, from ``time.perf_counter()``)."""

    data: MidiBytes
    timestamp: float | None = None


_LITTLE = sys.byteorder == "little"


def _pcm(samples: Samples, channels: int | None) -> tuple[bytes, bool, int]:
    """(native-endian bytes, is float32, channels) for a buffer of samples."""
    if isinstance(samples, (list, tuple, str)):
        raise TypeError("samples must be a buffer of int16 or float32 values (numpy array, array.array, bytes)")
    try:
        view = memoryview(samples)
    except TypeError:
        raise TypeError(
            "samples must be a buffer of int16 or float32 values (numpy array, array.array, bytes)"
        ) from None
    with view:
        if channels is None:
            channels = view.shape[1] if view.ndim == 2 and view.shape else 1
        elif view.ndim == 2 and view.shape and view.shape[1] != channels:
            raise ValueError(f"samples have {view.shape[1]} channels but channels is {channels}")
        if view.ndim > 2:
            raise ValueError("samples must be 1-D or 2-D (frames, channels)")
        fmt = view.format
        order = fmt[0] if fmt and fmt[0] in "@=<>!" else "@"
        code = fmt.lstrip("@=<>!")
        swap = (order in ">!") == _LITTLE and order not in "@="
        if code in ("B", "b", "c"):
            # Raw bytes: 16-bit little-endian PCM.
            data = view.tobytes()
            if len(data) % 2:
                raise ValueError("raw PCM bytes must be a whole number of 16-bit samples")
            if not _LITTLE:
                a = array.array("h", data)
                a.byteswap()
                data = a.tobytes()
            return data, False, channels
        if code == "h" and view.itemsize == 2:
            float32 = False
        elif code == "f" and view.itemsize == 4:
            float32 = True
        elif code == "d" and view.itemsize == 8:
            doubles = array.array("d", view.tobytes())
            if swap:
                doubles.byteswap()
            return array.array("f", doubles).tobytes(), True, channels
        else:
            raise TypeError(f"samples must be int16 or float32, got buffer format {fmt!r}")
        data = view.tobytes()
        if swap:
            a = array.array("f" if float32 else "h", data)
            a.byteswap()
            data = a.tobytes()
        return data, float32, channels


def _midi(data: MidiBytes) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        raise TypeError("MIDI data must be bytes, bytearray or a sequence of ints")
    try:
        return bytes(data)
    except (TypeError, ValueError):
        raise TypeError("MIDI data must be bytes, bytearray or a sequence of ints 0..255") from None
