from __future__ import annotations

import asyncio

import pytest
from helpers import record
from timecode_tools import FrameRate, MidiChunk, MtcReceiver, Timecode
from timecode_tools._testing import generate_mtc, mtc_full_frame

PERIOD = {"24": 1 / 24, "25": 1 / 25, "29.97df": 1001 / 30000, "30": 1 / 30}


def messages(stream, t0=1.0):
    """One chunk per quarter-frame message, timestamped like a MIDI API would."""
    for data, t in stream:
        yield MidiChunk(data, t0 + t)


@pytest.mark.parametrize(
    "frame_rate,start,first",
    [
        (FrameRate.FPS_24, "01:00:00:00", "01:00:00:03"),
        (FrameRate.FPS_25, "10:59:59:20", "10:59:59:23"),
        (FrameRate.FPS_2997_DROP, "00:00:59;26", "00:00:59;29"),
        (FrameRate.FPS_30, "23:59:59:26", "23:59:59:29"),
    ],
)
def test_one_frame_per_frame_from_quarter_frames(frame_rate, start, first):
    rx = MtcReceiver()
    log = record(rx)
    frames = []
    rx.on("frame", frames.append)
    for m in messages(generate_mtc(frame_rate, start, 40)):
        rx.push(m)
    assert log == [f"lock {first} {frame_rate}"]
    # 40 frames give 39 readings, of which 2 come before the lock.
    assert len(frames) == 37
    assert str(frames[0]) == first
    day = Timecode.from_frame_index(-1, frame_rate).frame_index() + 1
    for a, b in zip(frames, frames[1:], strict=False):
        assert (b.frame_index() - a.frame_index()) % day == 1
        assert abs(b.timestamp - a.timestamp - PERIOD[frame_rate.value]) < 1e-5
    assert rx.detected_rate is frame_rate
    rx.close()


def test_mixed_midi_traffic_and_byte_forms():
    stream = generate_mtc("25", "02:00:00:00", 8)
    noisy = [[0xF8, 0x90, 64, 100, 65, 0, data[0], 0xFE, data[1]] for data, _ in stream]
    for form in (list, bytes, bytearray, memoryview, tuple):
        rx = MtcReceiver()
        frames = []
        rx.on("frame", lambda tc, frames=frames: frames.append(str(tc)))
        for b, (_, t) in zip(noisy, stream, strict=False):
            rx.push(MidiChunk(form(b) if form is not memoryview else memoryview(bytes(b)), t))
        assert frames == ["02:00:00:03", "02:00:00:04", "02:00:00:05", "02:00:00:06", "02:00:00:07"]
        rx.close()


def test_bare_bytes_are_timestamped_on_arrival():
    rx = MtcReceiver()
    frames = []
    rx.on("frame", frames.append)
    for data, _ in generate_mtc("25", "00:00:00:00", 8):
        rx.push(data)
    assert len(frames) == 5 and all(tc.timestamp is not None for tc in frames)
    rx.close()


def test_locate_by_full_frame_relocate_stop():
    rx = MtcReceiver()
    log = record(rx)
    frames = []
    rx.on("frame", lambda tc, frames=frames: frames.append(str(tc)))
    for m in messages(generate_mtc("30", "00:00:58:00", 30)):
        rx.push(m)
    # Locate while running: the chase ends and the new position is a jump.
    rx.push(MidiChunk(mtc_full_frame("05:00:00:00", "30"), 5.0))
    assert str(rx.current) == "05:00:00:00"
    for m in messages(generate_mtc("30", "05:00:00:00", 30), 6.0):
        rx.push(m)
    # A relocation during playback (no full frame) is a jump; the chase goes on.
    for m in messages(generate_mtc("30", "07:00:00:00", 30), 7.0):
        rx.push(m)
    rx.push(MidiChunk(b"\xf8", 9.0))
    assert log == [
        "lock 00:00:58:03 30",
        "lost",
        "jump 05:00:00:00",
        "lock 05:00:00:03 30",
        "jump 07:00:00:02",
        "lost",
    ]
    assert frames[0] == "00:00:58:03"
    assert frames[-1] == "07:00:00:29"
    rx.close()


def test_attach_wait_for_and_iterate():
    stream = generate_mtc("25", "00:10:00:00", 50)

    async def source():
        for m in messages(stream):
            yield m

    async def main():
        rx = MtcReceiver(offset_frames=2)
        got = []

        async def first_three():
            async for tc in rx.frames(buffer="all"):
                got.append(str(tc))
                if len(got) == 3:
                    break

        task = asyncio.create_task(first_three())
        rates = []
        rx.on("lock", lambda info: rates.append(info.frame_rate))
        passed = rx.wait_for("00:10:01:00")
        await asyncio.sleep(0)
        rx.attach(source())
        await asyncio.wait_for(task, 10)
        assert got == ["00:10:00:05", "00:10:00:06", "00:10:00:07"]
        assert str(await asyncio.wait_for(passed, 10)) == "00:10:01:00"
        assert rates == ["25"]
        rx.detach()
        with pytest.raises(TypeError):
            rx.push(MidiChunk("F1", 0))
        rx.close()

    asyncio.run(main())
