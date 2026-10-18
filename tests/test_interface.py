"""LtcReceiver and MtcReceiver behave the same through TimecodeReceiver."""

from __future__ import annotations

import asyncio

import pytest
from helpers import chunks
from timecode_tools import LtcReceiver, MidiChunk, MtcReceiver, PcmChunk, ReceiverClosed, TimecodeReceiver
from timecode_tools._testing import generate_ltc, generate_mtc


async def ltc_chunks():
    for c in chunks(generate_ltc(start="00:59:59:00", frame_count=60), t0=0.0):
        yield c


async def mtc_chunks():
    for data, t in generate_mtc("25", "00:59:59:00", 60):
        yield MidiChunk(data, t)


async def follow(rx: TimecodeReceiver, source):
    """Written once against the shared interface."""
    assert isinstance(rx, TimecodeReceiver)
    log = []
    rx.on("lock", lambda info: log.append(f"lock {info.frame_rate}"))
    rx.on("lost", lambda: log.append("lost"))
    ended = asyncio.Event()
    rx.on("end", ended.set)
    passed = rx.wait_for("01:00:00:10")
    rx.attach(source)
    log.append(f"passed {await passed}")
    await ended.wait()
    return log


def test_the_same_code_follows_ltc_and_mtc():
    want = ["lock 25", "passed 01:00:00:10", "lost"]
    assert asyncio.run(follow(LtcReceiver(), ltc_chunks())) == want
    assert asyncio.run(follow(MtcReceiver(), mtc_chunks())) == want
    with pytest.raises(TypeError):
        TimecodeReceiver(None, None, 0.3)


def test_offset_frames_can_be_read_and_changed():
    for rx in (LtcReceiver(offset_frames=-2), MtcReceiver(offset_frames=-2)):
        assert rx.offset_frames == -2
        rx.offset_frames = 3
        assert rx.offset_frames == 3
        with pytest.raises(TypeError):
            rx.offset_frames = 1.5
        rx.close()
    rx = LtcReceiver()
    frames = []
    rx.on("frame", lambda tc: frames.append(str(tc)))
    pcm = generate_ltc(frame_count=20)
    rx.push(PcmChunk(pcm[:19200], 48000, timestamp=0.0))
    rx.offset_frames = 100
    rx.push(PcmChunk(pcm[19200:], 48000, timestamp=0.4))
    # The shift appears as a jump once confirmed, not as a stray frame.
    assert frames[5] == "00:00:00:08"
    assert frames[6] == "00:00:04:10"
    assert frames[-1] == "00:00:04:19"
    rx.close()


def test_close_ends_iterators_fails_waiters_and_stops_input():
    async def main():
        for rx in (LtcReceiver(), MtcReceiver()):
            it = rx.frames()
            ev = rx.events()
            waiting = rx.wait_for("10:00:00:00")
            rx.close()
            with pytest.raises(StopAsyncIteration):
                await it.__anext__()
            with pytest.raises(StopAsyncIteration):
                await ev.__anext__()
            with pytest.raises(ReceiverClosed, match="closed"):
                await waiting
            with pytest.raises(ReceiverClosed, match="closed"):
                await rx.wait_for("10:00:00:00")
            with pytest.raises(ReceiverClosed):
                rx.push(MidiChunk(b"\x00\x00"))
            with pytest.raises(ReceiverClosed):
                rx.attach(ltc_chunks())
            assert rx.closed
            rx.close()  # idempotent

    asyncio.run(main())


def test_close_while_locked_reports_lost_and_with_closes():
    log = []
    with LtcReceiver() as rx:
        rx.on("lost", lambda: log.append("lost"))
        rx.push(PcmChunk(generate_ltc(frame_count=20), 48000, timestamp=0.0))
        assert rx.locked
    assert rx.closed
    assert log == ["lost"]


def test_events_yields_end_and_raises_an_input_error():
    async def main():
        rx = LtcReceiver()
        seen = []

        async def watch():
            async for ev in rx.events():
                seen.append(ev.type)

        task = asyncio.create_task(watch())
        await asyncio.sleep(0)
        ended = asyncio.Event()
        rx.on("end", ended.set)
        rx.attach(ltc_chunks())
        await ended.wait()
        await asyncio.sleep(0)
        assert seen == ["lock", "lost", "end"]

        async def failing():
            async for c in ltc_chunks():
                yield c
            raise OSError("device unplugged")

        errors = []
        rx.on("error", errors.append)
        rx.attach(failing())
        with pytest.raises(OSError, match="device unplugged"):
            await asyncio.wait_for(task, 10)
        assert seen[3:] == ["lock"]  # the error ends the iteration
        assert len(errors) == 1
        rx.close()

    asyncio.run(main())


def test_a_thread_source_error_is_reported():
    import threading

    def failing():
        yield PcmChunk(generate_ltc(frame_count=2), 48000)
        raise OSError("gone")

    rx = MtcReceiver()
    got = []
    done = threading.Event()
    rx.on("error", got.append)
    rx.on("end", done.set)
    rx.attach(failing())
    assert done.wait(5)
    # The PcmChunk is not MIDI: the TypeError from push is the error.
    assert isinstance(got[0], TypeError)
    rx.close()
