from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from . import _native

if TYPE_CHECKING:
    from ._native import NativeTimecode


class FrameRate(str, enum.Enum):
    """Frame rates. ``AUTO`` detects the rate from the signal.

    Members are strings (``FrameRate.FPS_25 == "25"``), and everywhere a frame
    rate is taken, its value (``"auto"``, ``"24"``, ``"25"``, ``"29.97df"``,
    ``"30"``) works too.
    """

    AUTO = "auto"
    FPS_24 = "24"
    FPS_25 = "25"
    FPS_2997_DROP = "29.97df"
    FPS_30 = "30"

    def __str__(self) -> str:
        return self.value


FrameRateLike = FrameRate | str


class _Rate(NamedTuple):
    nominal: int  # label rate
    hz: float  # real frames per second
    drop: bool  # drop-frame labels


_RATES = {
    FrameRate.FPS_24: _Rate(24, 24.0, False),
    FrameRate.FPS_25: _Rate(25, 25.0, False),
    FrameRate.FPS_2997_DROP: _Rate(30, 30000 / 1001, True),
    FrameRate.FPS_30: _Rate(30, 30.0, False),
}


def _frame_rate(value: object, allow_auto: bool) -> FrameRate:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # 24, 25, 30
    try:
        rate = FrameRate(value)
    except ValueError:
        rate = None
    if rate is None or (rate is FrameRate.AUTO and not allow_auto):
        names = ", ".join(f"FrameRate.{r.name}" for r in FrameRate if allow_auto or r is not FrameRate.AUTO)
        raise ValueError(f"frame_rate must be one of {names}, got {value!r}")
    return rate


def _optional_rate(value: str | None) -> FrameRate | None:
    return None if value is None else FrameRate(value)


_TIMECODE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})([:;.,])(\d{2})$")


@dataclass(frozen=True, eq=False, repr=False)
class Timecode:
    """A timecode label.

    ``frame_rate`` is the rate the label is read at (``None`` if unknown).
    ``timestamp``, set on timecodes from a receiver, is when the frame began,
    in seconds on the clock of the chunk timestamps.

    Equality compares the digits and the drop-frame flag. ``<``, ``>`` and
    :meth:`compare` order by the digits only; neither looks at the rate or
    the timestamp.
    """

    hours: int
    minutes: int
    seconds: int
    frames: int
    drop_frame: bool = False
    frame_rate: FrameRate | None = field(default=None)
    timestamp: float | None = field(default=None)

    def __str__(self) -> str:
        sep = ";" if self.drop_frame else ":"
        return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}{sep}{self.frames:02d}"

    def __repr__(self) -> str:
        extra = ""
        if self.frame_rate is not None:
            extra += f", frame_rate=FrameRate.{self.frame_rate.name}"
        if self.timestamp is not None:
            extra += f", timestamp={self.timestamp!r}"
        return f"Timecode('{self}'{extra})"

    def _key(self) -> int:
        return ((self.hours * 60 + self.minutes) * 60 + self.seconds) * 100 + self.frames

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Timecode):
            return NotImplemented
        return self._key() == other._key() and self.drop_frame == other.drop_frame

    def __hash__(self) -> int:
        return hash((self._key(), self.drop_frame))

    def __lt__(self, other: Timecode) -> bool:
        return self.compare(other) < 0

    def __le__(self, other: Timecode) -> bool:
        return self.compare(other) <= 0

    def __gt__(self, other: Timecode) -> bool:
        return self.compare(other) > 0

    def __ge__(self, other: Timecode) -> bool:
        return self.compare(other) >= 0

    def compare(self, other: Timecode) -> int:
        """-1, 0 or 1, ordering by the digits."""
        if not isinstance(other, Timecode):
            raise TypeError(f"cannot compare Timecode with {type(other).__name__}")
        a, b = self._key(), other._key()
        return (a > b) - (a < b)

    def frame_index(self, frame_rate: FrameRateLike | None = None) -> int:
        """Frames since 00:00:00:00 at ``frame_rate`` (default: this timecode's rate)."""
        if frame_rate is None:
            frame_rate = self.frame_rate
        if frame_rate is None:
            raise ValueError("frame_index() needs a frame rate")
        rate = _RATES[_frame_rate(frame_rate, False)]
        label = (self.hours, self.minutes, self.seconds, self.frames, self.drop_frame)
        return _native.frame_index(label, rate.nominal)

    @classmethod
    def parse(cls, text: str, frame_rate: FrameRateLike | None = None) -> Timecode:
        """Parses ``HH:MM:SS:FF`` (``;`` before the frames means drop-frame).

        With a frame rate, the drop-frame flag follows the rate and the frame
        number must exist at that rate. Raises ``ValueError`` otherwise.
        """
        m = _TIMECODE.match(str(text))
        if not m:
            raise ValueError(f"expected HH:MM:SS:FF, got {text!r}")
        hours, minutes, seconds, frames = (int(m.group(i)) for i in (1, 2, 3, 5))
        drop_frame = m.group(4) != ":"
        rate: FrameRate | None = None
        if frame_rate is not None:
            rate = _frame_rate(frame_rate, True)
            if rate is FrameRate.AUTO:
                rate = None
        if rate is not None:
            if drop_frame and not _RATES[rate].drop:
                raise ValueError(f"{text} is drop-frame but the frame rate is {rate}")
            drop_frame = _RATES[rate].drop
        nominal = _RATES[rate].nominal if rate else 30
        skipped = drop_frame and rate is not None and seconds == 0 and minutes % 10 != 0 and frames < 2
        if hours > 23 or minutes > 59 or seconds > 59 or frames >= nominal or skipped:
            where = f" at {rate}" if rate else ""
            raise ValueError(f"{text} is not a valid timecode{where}")
        return cls(hours, minutes, seconds, frames, drop_frame, rate)

    @classmethod
    def from_frame_index(cls, index: int, frame_rate: FrameRateLike) -> Timecode:
        """Timecode for a frame count since 00:00:00:00 (wraps around one day)."""
        rate = _frame_rate(frame_rate, False)
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"index must be an int, got {index!r}")
        r = _RATES[rate]
        index = index % _native.frames_per_day(r.nominal, r.drop)
        h, m, s, f, df = _native.label_from_index(index, r.nominal, r.drop)
        return cls(h, m, s, f, df, rate)


def _from_native(t: NativeTimecode) -> Timecode:
    h, m, s, f, df, rate, _locked, timestamp, _sample, _channel = t
    return Timecode(h, m, s, f, df, _optional_rate(rate), timestamp)
