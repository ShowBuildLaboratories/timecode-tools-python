from __future__ import annotations

from dataclasses import dataclass

from . import _native
from ._chunks import Samples, _pcm
from ._timecode import FrameRate, FrameRateLike, Timecode, _frame_rate, _from_native, _optional_rate
from ._validate import check_int, check_offset, check_timestamp


@dataclass(frozen=True)
class DecodedFrame:
    """A frame from :meth:`LtcDecoder.push`."""

    timecode: Timecode
    locked: bool
    frame_rate: FrameRate | None
    #: Index (per channel) of the sample where the frame began.
    sample_index: int | None


class LtcDecoder:
    """Low-level LTC decoder: push interleaved PCM, get back the frames
    completed in that block, locked or not. No events, threads or timers."""

    def __init__(
        self,
        sample_rate: int,
        channels: int = 1,
        channel: int = 1,
        frame_rate: FrameRateLike = FrameRate.AUTO,
        offset_frames: int = 0,
    ) -> None:
        check_int("sample_rate", sample_rate, 8000, 768000)
        check_int("channels", channels, 1, 64)
        check_int("channel", channel, 1, channels)
        rate = _frame_rate(frame_rate, True)
        check_offset(offset_frames)
        self._args = (sample_rate, channels, channel, rate.value, offset_frames)
        self._native = _native.NativeLtcDecoder(*self._args)
        self.sample_rate = sample_rate
        self.channels = channels

    def push(self, samples: Samples, timestamp: float | None = None) -> list[DecodedFrame]:
        """Decodes a block of interleaved samples (see :class:`PcmChunk` for
        the accepted types) and returns each frame completed in it.

        ``timestamp`` (optional) is the monotonic time in seconds of the
        block's first sample; frames then carry a ``timestamp`` too.
        """
        data, float32, _ = _pcm(samples, self.channels)
        check_timestamp(timestamp, allow_none=True)
        frames = self._native.push(data, float32, timestamp)
        return [DecodedFrame(_from_native(f), f[6], _optional_rate(f[5]), f[8]) for f in frames]

    def reset(self) -> None:
        """Clears all decoding state; sample counting restarts at zero."""
        self._native = _native.NativeLtcDecoder(*self._args)

    @property
    def locked(self) -> bool:
        return self._native.locked

    @property
    def frame_rate(self) -> FrameRate | None:
        """The configured rate, or the detected one with ``AUTO`` (``None`` if unknown)."""
        return _optional_rate(self._native.frame_rate)

    @property
    def signal_level(self) -> float:
        """Peak input level, 0..1."""
        return self._native.signal_level

    @property
    def samples_decoded(self) -> int:
        """Samples per channel decoded so far."""
        return self._native.samples_decoded
