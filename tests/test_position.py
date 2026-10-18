"""position_at() / frames_at(): the timecode at any instant, from a fit of the
recent signal."""

from __future__ import annotations

import pytest
from helpers import RATE, chunks, concat, silence
from timecode_tools import FrameRate, LtcReceiver, MidiChunk, MtcReceiver, Timecode
from timecode_tools._testing import generate_ltc, generate_mtc


def feed(rx, pcm):
    for c in chunks(pcm):
        rx.push(c)


def frames_of(rx):
    frames = []
    rx.on("frame", frames.append)
    return frames


def test_ltc_position_at_matches_the_frame_timestamps():
    rx = LtcReceiver()
    frames = frames_of(rx)
    assert rx.frames_at(1.0) is None
    assert rx.position_at(1.0) is None
    feed(rx, generate_ltc(start="01:00:00:00", frame_count=50))
    last = frames[-1]
    assert abs(rx.frames_at(last.timestamp) - last.frame_index()) <= 0.25
    at = rx.position_at(last.timestamp)
    assert isinstance(at, Timecode)
    assert str(at) == str(last)
    assert at.frame_rate is FrameRate.FPS_25
    assert at.timestamp == last.timestamp
    for tc in frames[10:]:
        assert str(rx.position_at(tc.timestamp)) == str(tc)
    assert str(rx.position_at(last.timestamp + 0.020)) == str(last)
    assert abs(rx.frames_at(last.timestamp + 0.4) - (last.frame_index() + 10)) <= 0.25
    rx.close()


def test_ltc_frames_at_is_not_wrapped_at_midnight():
    rx = LtcReceiver()
    frames = frames_of(rx)
    feed(rx, generate_ltc(frame_rate="30", start="23:59:59:00", frame_count=60))
    last = frames[-1]
    assert str(last).startswith("00:00:00:")
    day = Timecode.from_frame_index(-1, "30").frame_index() + 1
    assert abs(rx.frames_at(last.timestamp) - (day + last.frame_index())) <= 0.25
    assert str(rx.position_at(last.timestamp)) == str(last)
    rx.close()


def test_ltc_offset_frames_is_applied():
    rx = LtcReceiver()
    frames = frames_of(rx)
    feed(rx, generate_ltc(start="01:00:00:00", frame_count=50))
    t = frames[-1].timestamp
    before = rx.frames_at(t)
    rx.offset_frames = 2
    assert abs(rx.frames_at(t) - (before + 2)) < 1e-9
    rx.close()


def test_ltc_none_after_loss_and_after_close():
    ltc = generate_ltc(start="01:00:00:00", frame_count=50)
    rx = LtcReceiver()
    frames = frames_of(rx)
    feed(rx, concat(ltc, silence(RATE)))
    assert not rx.locked
    assert rx.frames_at(frames[-1].timestamp) is None
    assert rx.position_at(frames[-1].timestamp) is None
    rx.close()

    rx = LtcReceiver()
    frames = frames_of(rx)
    feed(rx, ltc)
    t = frames[-1].timestamp
    assert rx.frames_at(t) is not None
    rx.close()
    assert rx.frames_at(t) is None
    assert rx.position_at(t) is None


def test_mtc_position_at_after_lock():
    rx = MtcReceiver()
    frames = frames_of(rx)
    for data, t in generate_mtc("30", "00:00:00:00", 30):
        rx.push(MidiChunk(data, 1 + t))
    assert rx.locked
    last = frames[-1]
    assert str(rx.position_at(last.timestamp)) == str(last)
    assert abs(rx.frames_at(last.timestamp) - last.frame_index()) <= 0.25
    rx.close()


@pytest.mark.parametrize("bad", [None, -1, float("nan"), float("inf"), "10", True])
def test_the_timestamp_is_checked(bad):
    rx = MtcReceiver()
    with pytest.raises(ValueError):
        rx.frames_at(bad)
    with pytest.raises(ValueError):
        rx.position_at(bad)
    rx.close()
