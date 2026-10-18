from __future__ import annotations

import array
import asyncio
import threading
import time

import pytest
from helpers import RATE, chunks, concat, record, silence
from timecode_tools import FrameRate, LtcReceiver, PcmChunk, ReceiverClosed
from timecode_tools._testing import generate_ltc


def frames_of(rx):
    frames = []
    rx.on("frame", lambda tc: frames.append(str(tc)))
    return frames


def test_lock_frames_jump_and_loss_from_pushed_chunks():
    pcm = concat(
        generate_ltc(start="01:00:00:00", frame_count=40),
        generate_ltc(start="02:00:00:00", frame_count=40),
        silence(RATE),
    )
    rx = LtcReceiver()
    log = record(rx)
    frames = frames_of(rx)
    for c in chunks(pcm):
        rx.push(c)
    assert log == ["lock 01:00:00:03 25", "jump 02:00:00:01", "lost"]
    assert frames[0] == "01:00:00:03"
    assert "01:00:01:14" in frames
    assert frames[-1] == "02:00:01:14"
    assert not rx.locked
    rx.close()


def test_a_single_corrupt_frame_is_ignored():
    a = generate_ltc(frame_rate="30", start="00:10:00:00", frame_count=20)
    glitch = generate_ltc(frame_rate="30", start="05:00:00:00", frame_count=1)
    b = generate_ltc(frame_rate="30", start="00:10:00:21", frame_count=20)
    rx = LtcReceiver()
    log = record(rx)
    frames = frames_of(rx)
    for c in chunks(concat(a, glitch, b)):
        rx.push(c)
    assert log == ["lock 00:10:00:03 30"]
    assert not any(f.startswith("05") for f in frames)
    assert frames[-1] == "00:10:01:10"
    rx.close()


def test_attach_an_iterable_on_a_thread_and_wait_for():
    pcm = generate_ltc(frame_rate=FrameRate.FPS_2997_DROP, start="00:00:58;00", frame_count=120)
    rx = LtcReceiver()
    log = record(rx)
    passed = rx.wait_for("00:01:00;02")
    never = rx.wait_for("10:00:00:00")
    ended = threading.Event()
    rx.on("end", ended.set)
    rx.attach(chunks(pcm))
    tc = passed.result(timeout=10)
    assert str(tc) == "00:01:00;02"
    assert tc.frame_rate is FrameRate.FPS_2997_DROP
    assert tc.timestamp > 1
    assert ended.wait(10)
    assert log == ["lock 00:00:58;03 29.97df", "lost"]
    assert not rx.attached
    assert never.cancel()
    rx.close()


def test_await_wait_for_with_an_async_source():
    pcm = generate_ltc(frame_rate="24", start="00:59:59:00", frame_count=60)

    async def source():
        for c in chunks(pcm, block=4800):
            yield c
            await asyncio.sleep(0)

    async def main():
        rx = LtcReceiver(offset_frames=1)
        at = rx.wait_for("01:00:00:00")
        rx.attach(source())
        assert str(await asyncio.wait_for(at, 10)) == "01:00:00:00"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(rx.wait_for("12:00:00:00"), 0.05)
        rx.close()

    asyncio.run(main())


def test_attach_needs_an_event_loop_for_async_sources_and_an_iterable():
    async def source():
        yield None

    rx = LtcReceiver()
    gen = source()
    with pytest.raises(RuntimeError):
        rx.attach(gen)
    asyncio.run(gen.aclose())
    for bad in (None, 3, b"abc", PcmChunk(b"", RATE)):
        with pytest.raises(TypeError):
            rx.attach(bad)
    rx.close()


def test_detach_stops_reading_and_reports_loss():
    pcm = generate_ltc(frame_count=50)

    async def main():
        pulled = 0

        async def slow():
            nonlocal pulled
            for c in chunks(pcm):
                pulled += 1
                yield c
                await asyncio.sleep(0)

        rx = LtcReceiver()
        log = record(rx)
        locked = asyncio.Event()
        rx.on("lock", lambda _: locked.set())
        rx.attach(slow())
        await locked.wait()
        rx.detach()
        at = pulled
        await asyncio.sleep(0.02)
        assert pulled <= at + 1
        assert log[-1] == "lost"
        assert not rx.attached
        rx.close()

    asyncio.run(main())


def test_loss_is_reported_when_chunks_stop_arriving():
    rx = LtcReceiver(lost_after=0.1)
    lost = threading.Event()
    rx.on("lost", lost.set)
    for c in chunks(generate_ltc(frame_count=20)):
        rx.push(c)
    assert rx.locked
    started = time.monotonic()
    assert lost.wait(5)
    assert time.monotonic() - started >= 0.09
    assert not rx.locked
    rx.close()


def test_missing_timestamps_fall_back_to_arrival_time():
    rx = LtcReceiver()
    stamps = []
    rx.on("frame", lambda tc: stamps.append(tc.timestamp))
    before = time.perf_counter()
    rx.push(PcmChunk(generate_ltc(frame_count=10), RATE))
    assert stamps and all(before - 1 < t < time.perf_counter() + 1 for t in stamps)
    rx.close()


def test_stereo_chunks_select_the_ltc_channel():
    ltc = generate_ltc(frame_count=30)
    stereo = array.array("h", bytes(4 * len(ltc)))
    stereo[1::2] = ltc
    for channel, want in [(2, [2]), (1, [])]:
        rx = LtcReceiver(channel=channel)
        locks = []
        rx.on("lock", lambda info, locks=locks: locks.append(info.channel))
        for c in chunks(stereo, block=960, channels=2):
            rx.push(c)
        assert locks == want
        assert rx.channel == channel
        rx.close()


def test_on_off_once_and_decorator():
    pcm = concat(
        generate_ltc(start="01:00:00:00", frame_count=30),
        generate_ltc(start="03:00:00:00", frame_count=30),
        silence(RATE),
    )
    rx = LtcReceiver()
    seen = []

    def on_frame(tc):
        seen.append(f"frame {tc}")

    rx.on("frame", on_frame)

    @rx.on("lock")
    def on_lock(info):
        seen.append(f"lock {info.frame_rate}")

    rx.on("jump", lambda tc: seen.append(f"jump {tc}"))
    rx.on("lost", lambda: seen.append("lost"), once=True)
    for i, c in enumerate(chunks(pcm)):
        rx.push(c)
        if i == 40:
            rx.off("frame", on_frame)
    assert seen[0] == "lock 25"
    assert seen[1] == "frame 01:00:00:03"
    assert "jump 03:00:00:01" in seen
    assert not any(s.startswith("frame 03") for s in seen)
    assert seen[-1] == "lost"
    assert rx.listener_count("frame") == 0
    assert rx.listener_count("lost") == 0
    with pytest.raises(ValueError):
        rx.on("frames", print)
    rx.close()


def test_a_failing_listener_does_not_stop_the_others(caplog):
    rx = LtcReceiver()
    frames = []
    rx.on("frame", lambda tc: 1 / 0)
    rx.on("frame", frames.append)
    for c in chunks(generate_ltc(frame_count=10)):
        rx.push(c)
    assert len(frames) == 7
    assert "listener raised" in caplog.text
    rx.close()


def test_frames_and_events_as_async_iterators():
    pcm = concat(
        generate_ltc(frame_rate="30", start="00:00:10:00", frame_count=30),
        generate_ltc(frame_rate="30", start="00:20:00:00", frame_count=30),
        silence(RATE),
    )

    async def main():
        rx = LtcReceiver()
        got, events = [], []

        async def all_frames():
            async for tc in rx.frames(buffer="all"):
                got.append(str(tc))
                if str(tc) == "00:20:00:20":
                    break

        async def all_events():
            async for ev in rx.events():
                events.append(ev.type if ev.type == "lost" else f"{ev.type} {ev.timecode}")
                if ev.type == "lost":
                    break

        tasks = [asyncio.create_task(all_frames()), asyncio.create_task(all_events())]
        # A slow consumer with the default buffer gets the newest frame only.
        latest = rx.frames()
        await asyncio.sleep(0)
        rx.attach(iter(chunks(pcm)))  # read on a thread
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert got[0] == "00:00:10:03"
        assert len(got) == 27 + 20
        assert events == ["lock 00:00:10:03", "jump 00:20:00:01", "lost"]
        await asyncio.sleep(0.05)
        assert str(await latest.__anext__()) == "00:20:00:29"
        await latest.aclose()
        await asyncio.sleep(0)
        assert rx._subscriptions == []
        with pytest.raises(ValueError):
            rx.frames(buffer="some")
        rx.close()

    asyncio.run(main())


def test_push_after_close():
    rx = LtcReceiver()
    rx.close()
    with pytest.raises(ReceiverClosed):
        rx.push(PcmChunk(generate_ltc(frame_count=2), RATE))


def test_push_checks_the_chunk():
    rx = LtcReceiver()
    with pytest.raises(TypeError):
        rx.push(generate_ltc(frame_count=2))
    with pytest.raises(ValueError):
        rx.push(PcmChunk(generate_ltc(frame_count=2), 100))
    with pytest.raises(ValueError):
        rx.push(PcmChunk(generate_ltc(frame_count=2), RATE, timestamp=float("nan")))
    with pytest.raises(ValueError):
        LtcReceiver(lost_after=0)
    with pytest.raises(ValueError):
        LtcReceiver(channel=65)
    rx.close()
