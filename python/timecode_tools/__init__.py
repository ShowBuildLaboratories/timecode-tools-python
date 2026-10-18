"""SMPTE LTC and MIDI Time Code (MTC) readers.

Stream in PCM audio or MIDI bytes; get timecode, lock, loss and jump
events. The decoding runs in the Rust crate ``timecode-tools``.
"""

from importlib.metadata import PackageNotFoundError, version

from ._chunks import MidiChunk, PcmChunk
from ._decoder import DecodedFrame, LtcDecoder
from ._receiver import (
    LockInfo,
    LtcReceiver,
    MtcReceiver,
    ReceiverClosed,
    ReceiverEvent,
    TimecodeReceiver,
    TimecodeWait,
)
from ._timecode import FrameRate, Timecode

try:
    __version__ = version("timecode-tools")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0+unknown"

__all__ = [
    "DecodedFrame",
    "FrameRate",
    "LockInfo",
    "LtcDecoder",
    "LtcReceiver",
    "MidiChunk",
    "MtcReceiver",
    "PcmChunk",
    "ReceiverClosed",
    "ReceiverEvent",
    "Timecode",
    "TimecodeReceiver",
    "TimecodeWait",
]
