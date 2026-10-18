from __future__ import annotations

import array

import numpy as np
import pytest
from timecode_tools import FrameRate, LtcDecoder, Timecode
from timecode_tools._testing import generate_ltc


def decode_all(decoder, pcm, block=1000):
    out = []
    for i in range(0, len(pcm), block):
        out.extend(decoder.push(pcm[i : i + block]))
    return out


@pytest.mark.parametrize(
    "frame_rate,start",
    [
        (FrameRate.FPS_24, "10:00:00:00"),
        (FrameRate.FPS_25, "01:02:03:04"),
        (FrameRate.FPS_2997_DROP, "00:00:59;20"),
        (FrameRate.FPS_30, "23:59:59:00"),
    ],
)
def test_decodes_every_frame(frame_rate, start):
    pcm = generate_ltc(frame_rate=frame_rate, start=start, frame_count=60)
    decoder = LtcDecoder(48000)
    frames = decode_all(decoder, pcm, 777)
    assert len(frames) == 60
    assert str(frames[0].timecode) == start
    first = Timecode.parse(start, frame_rate).frame_index()
    day = Timecode.from_frame_index(-1, frame_rate).frame_index() + 1
    for i, f in enumerate(frames):
        assert f.frame_rate == frame_rate
        assert f.timecode.frame_rate == frame_rate
        assert f.locked == (i >= 3)
        assert f.timecode.frame_index() == (first + i) % day
    assert decoder.locked
    assert decoder.frame_rate == frame_rate
    assert decoder.signal_level > 0.5


def test_sample_formats_decode_alike():
    pcm = generate_ltc(frame_count=20)
    want = [str(f.timecode) for f in LtcDecoder(48000).push(pcm)]
    assert len(want) == 20
    as_np = np.frombuffer(pcm, dtype=np.int16)
    for samples in [
        as_np,
        as_np.astype(np.float32) / 32767,
        as_np.astype(np.float64) / 32767,
        as_np.astype(">i2"),
        array.array("f", (s / 32767 for s in pcm)),
        pcm.tobytes() if np.little_endian else as_np.astype("<i2").tobytes(),
        bytearray(as_np.astype("<i2").tobytes()),
        memoryview(pcm),
    ]:
        assert [str(f.timecode) for f in LtcDecoder(48000).push(samples)] == want


def test_two_dimensional_arrays_give_the_channel_count():
    ltc = np.frombuffer(generate_ltc(frame_count=20), dtype=np.int16)
    stereo = np.zeros((len(ltc), 2), dtype=np.int16)
    stereo[:, 1] = ltc
    assert len(LtcDecoder(48000, channels=2, channel=2).push(stereo)) == 20
    with pytest.raises(ValueError, match="channels"):
        LtcDecoder(48000).push(stereo)


def test_sample_index_and_timestamps():
    pcm = generate_ltc(frame_count=30)
    decoder = LtcDecoder(48000)
    frames = []
    for i in range(0, len(pcm), 480):
        # 10 ms blocks with a timestamp 5 s into some monotonic clock.
        frames.extend(decoder.push(pcm[i : i + 480], 5 + i / 48000))
    for a, b in zip(frames, frames[1:], strict=False):
        assert b.sample_index - a.sample_index == 1920
    # Frame n of the generated signal starts after an 8-bit preamble.
    preamble = 8 / 80 * 0.040
    for n, f in enumerate(frames):
        assert abs(f.timecode.timestamp - (5 + preamble + n * 0.040)) < 0.0002, f.timecode


def test_offset_frames_shifts_the_label():
    pcm = generate_ltc(frame_rate=FrameRate.FPS_2997_DROP, start="00:00:59;28", frame_count=10)
    assert str(LtcDecoder(48000).push(pcm)[0].timecode) == "00:00:59;28"
    assert str(LtcDecoder(48000, offset_frames=2).push(pcm)[0].timecode) == "00:01:00;02"
    back = LtcDecoder(48000, offset_frames=-1).push(generate_ltc(frame_count=2))
    assert str(back[0].timecode) == "23:59:59:24"


def test_fixed_frame_rate():
    frames = LtcDecoder(48000, frame_rate="25").push(generate_ltc(frame_count=10))
    assert all(f.frame_rate is FrameRate.FPS_25 for f in frames)


def test_multichannel_reads_the_selected_channel():
    ltc = generate_ltc(frame_count=20, start="05:00:00:00")
    channels = 4
    pcm = array.array("h", bytes(2 * len(ltc) * channels))
    for i, v in enumerate(ltc):
        pcm[i * channels] = round(np.sin(i * 0.03) * 20000)  # music-like
        pcm[i * channels + 2] = v
    third = LtcDecoder(48000, channels=channels, channel=3)
    assert len(decode_all(third, pcm, 4 * 512)) == 20
    assert len(decode_all(LtcDecoder(48000, channels=channels), pcm, 4 * 512)) == 0
    with pytest.raises(ValueError, match="multiple of 4"):
        third.push(array.array("h", [0, 0, 0]))


def test_silence_and_noise_give_nothing():
    decoder = LtcDecoder(44100)
    assert decoder.push(array.array("h", bytes(2 * 44100))) == []
    noise = np.random.default_rng(7).integers(-32768, 32767, 44100, dtype=np.int16)
    assert decoder.push(noise) == []
    assert not decoder.locked
    assert decoder.samples_decoded == 88200
    decoder.reset()
    assert decoder.samples_decoded == 0


def test_timecode_parse_format_compare_and_arithmetic():
    assert str(Timecode.parse("01:02:03:04")) == "01:02:03:04"
    assert Timecode.parse("00:01:00;02").frame_index(FrameRate.FPS_2997_DROP) == 1800
    assert Timecode.parse("01:00:00:00", FrameRate.FPS_2997_DROP).frame_index() == 107892
    assert Timecode.parse("01:00:00:00", "25").frame_index() == 90000
    assert Timecode.parse("01:00:00:00", 25).frame_index() == 90000
    assert str(Timecode.from_frame_index(17982, FrameRate.FPS_2997_DROP)) == "00:10:00;00"
    assert str(Timecode.from_frame_index(-1, FrameRate.FPS_24)) == "23:59:59:23"
    assert str(Timecode.from_frame_index(10**30, FrameRate.FPS_25)) == str(
        Timecode.from_frame_index(10**30 % 2160000, FrameRate.FPS_25)
    )
    a, b = Timecode.parse("01:00:00:00"), Timecode.parse("00:59:59:29")
    assert a.compare(b) == 1 and b.compare(a) == -1 and a.compare(Timecode.parse("01:00:00:00")) == 0
    assert b < a and a >= b and max(a, b) is a
    assert a == Timecode(1, 0, 0, 0, frame_rate=FrameRate.FPS_25, timestamp=3.0)
    assert a != Timecode.parse("01:00:00;00")
    assert len({a, Timecode.parse("01:00:00:00", "30")}) == 1
    assert repr(Timecode.parse("01:00:00:00", "25")) == "Timecode('01:00:00:00', frame_rate=FrameRate.FPS_25)"
    assert f"{Timecode.parse('10:00:00;00')}" == "10:00:00;00"
    for bad, rate in [("00:01:00;00", "29.97df"), ("00:00:00:25", "25"), ("00:00:00;10", "25"), ("1:00:00:00", None)]:
        with pytest.raises(ValueError):
            Timecode.parse(bad, rate)
    with pytest.raises(ValueError, match="needs a frame rate"):
        Timecode.parse("01:00:00:00").frame_index()
    with pytest.raises(ValueError):
        Timecode.from_frame_index(0, FrameRate.AUTO)


def test_option_validation():
    with pytest.raises(ValueError):
        LtcDecoder(100)
    with pytest.raises(ValueError):
        LtcDecoder(48000, frame_rate=29.97)
    with pytest.raises(ValueError):
        LtcDecoder(48000, channels=2, channel=3)
    with pytest.raises(TypeError):
        LtcDecoder(48000.0)
    with pytest.raises(TypeError):
        LtcDecoder(48000).push([1, 2, 3])
    with pytest.raises(TypeError):
        LtcDecoder(48000).push(array.array("i", [1, 2, 3]))
    with pytest.raises(ValueError):
        LtcDecoder(48000).push(b"\x00\x00\x00")
    with pytest.raises(ValueError):
        LtcDecoder(48000).push(b"\x00\x00", timestamp=-1)
