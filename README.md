# timecode-tools

Timecode readers for Python: **SMPTE LTC** from PCM audio and **MIDI Time
Code** from MIDI bytes. Stream input in; get timecode, lock, loss and jump
events. The core is Rust
([`timecode-tools` crate](https://crates.io/crates/timecode-tools)) and ships
as prebuilt wheels, so no compiler is needed.

- `LtcReceiver` and `MtcReceiver` share one interface, `TimecodeReceiver`:
  the same `Timecode`, `FrameRate`, events, `wait_for()` and three ways of
  listening (callbacks, async iterators, futures)
- 24, 25, 29.97 drop-frame and 30 fps, detected automatically
- Any sample rate, int16 or float32 samples (numpy arrays, `array.array`,
  raw bytes), mono or multichannel
- MTC quarter-frame and full-frame (SysEx) messages, picked out of any MIDI
  traffic, with a reading every frame
- Smooths delivery jitter using chunk timestamps, so each frame carries an
  accurate start time
- Rejects single corrupt frames; reports real jumps (relocates) after two
  contiguous frames
- Works from audio callbacks and threads, or from asyncio
- Same results as the Node.js package
  [`timecode-tools`](https://www.npmjs.com/package/timecode-tools) and the
  Dart package [`timecode_tools`](https://pub.dev/packages/timecode_tools)
- Typed (`py.typed`); no runtime dependencies

```sh
pip install timecode-tools
```

Python 3.10 or later. Prebuilt for macOS (arm64, x64), Windows (x64, arm64)
and Linux (x64, arm64, armv7; glibc and musl).

## LTC

The library does not open audio devices. You deliver audio as **PCM chunks**
and the receiver follows the timecode in it. For example, with
[`sounddevice`](https://pypi.org/project/sounddevice/):

```python
import sounddevice as sd
from timecode_tools import FrameRate, LtcReceiver, PcmChunk

rx = LtcReceiver(
    frame_rate=FrameRate.AUTO,  # or a fixed rate
    offset_frames=0,            # latency compensation
    channel=1,                  # which channel carries LTC
)
rx.on("frame", lambda tc: print(tc))  # 01:02:03:04

def callback(indata, frames, time, status):
    # indata: float32 array of shape (frames, channels). The capture time is
    # on PortAudio's clock; if your host API reports 0 there, pass
    # timestamp=None (the arrival time) instead.
    rx.push(PcmChunk(indata, 48000, timestamp=time.inputBufferAdcTime))

with sd.InputStream(samplerate=48000, channels=2, callback=callback):
    rx.wait_for("01:00:10:00").result()  # blocks until playback passes it
rx.close()
```

`push()` is safe to call from any thread, such as an audio callback; the
callbacks you registered with `on()` run before it returns.

A chunk is a `PcmChunk`:

```python
@dataclass
class PcmChunk:
    samples: Any                    # interleaved int16 or float32 (-1..1)
    sample_rate: int                # Hz
    channels: int | None = None     # default: shape[1] of a 2-D array, else 1
    timestamp: float | None = None  # s, monotonic clock
```

`samples` can be any buffer of int16 or float32 values: a numpy array (1-D,
or 2-D `(frames, channels)` as audio libraries deliver it), an
`array.array('h')` or `array.array('f')`, or a `memoryview`. float64 is
converted. `bytes` and `bytearray` are read as 16-bit little-endian samples,
the format of `ffmpeg -f s16le`.

`timestamp` is the time the chunk's **first sample** was captured, in seconds
on a monotonic clock: the capture time your audio library reports (as
above), or `time.perf_counter()`. The receiver fits a line through these
stamps to give every frame an accurate `timestamp`. If you leave it out, the
time the chunk is received (`time.perf_counter()`) is used instead. That is
less precise: it includes whatever buffering and scheduling delay happened
before your code saw the audio, and frames inherit that delay and its
jitter.

`rx.level` is the recent peak input level of the LTC channel (0..1).

## MTC

`MtcReceiver` takes MIDI bytes: `MidiChunk(data, timestamp)` with the
monotonic time (s) the bytes arrived, or bare `bytes`, `bytearray` or lists
of ints (timestamped on arrival). Other MIDI messages, running status and
real-time bytes in between are skipped.

```python
import time
import mido  # with python-rtmidi
from timecode_tools import MidiChunk, MtcReceiver

rx = MtcReceiver(offset_frames=0)
rx.on("frame", lambda tc: print(tc))
rx.on("jump", lambda tc: print("locate", tc))

port = mido.open_input(
    "IAC Driver Bus 1",
    callback=lambda msg: rx.push(MidiChunk(msg.bytes(), time.perf_counter())),
)
```

Eight quarter-frame messages, sent over two frames, carry one timecode; the
receiver reports every frame, with `timestamp` set to when the frame began.
A full-frame message (sent by most devices when they locate) ends any chase
and is reported as a `jump`, and `rx.current` becomes the new position.

## Feeding input

There are two ways in, and they can be mixed with every way of listening:

- **`push(chunk)`** from your own code or callbacks, on any thread.
- **`attach(source)`** to have the receiver read a source until it ends:
  - an **async iterable** of chunks is read by a task on the running event
    loop (call `attach` from a coroutine);
  - any **other iterable** (a generator reading a file, a socket or
    `ffmpeg`) is read on a daemon thread.

  When the source finishes, `lost` is reported if locked, then `end`. If it
  raises, `error` is reported. `detach()` stops reading.

```python
import subprocess, time
from timecode_tools import LtcReceiver, PcmChunk

# macOS: list devices with `ffmpeg -f avfoundation -list_devices true -i ""`
ff = subprocess.Popen(
    ["ffmpeg", "-f", "avfoundation", "-i", ":0", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
    stdout=subprocess.PIPE,
)

def chunks():
    while data := ff.stdout.read1(4800):
        data = data[: len(data) & ~1]  # whole samples (a real reader keeps the odd byte)
        n = len(data) // 2
        yield PcmChunk(data, 48000, timestamp=time.perf_counter() - n / 48000)

rx = LtcReceiver()
rx.on("frame", print)
rx.attach(chunks())
```

(The command-line example below has a complete version.)

## One interface for both

Code written against `TimecodeReceiver` works with either receiver:

```python
from timecode_tools import LtcReceiver, MtcReceiver, TimecodeReceiver

def follow(rx: TimecodeReceiver) -> None:
    rx.on("frame", update)

follow(LtcReceiver(channel=2))
follow(MtcReceiver())
```

| Member | |
| --- | --- |
| `push(chunk)` | Deliver input from any thread |
| `attach(source)` / `detach()` / `close()` | Read a source, stop reading (reports `lost` if locked), or finish for good (ends iterators, fails pending `wait_for`); also a context manager (`with LtcReceiver() as rx:`) |
| `on(event, callback)` / `off(event, callback)` | Events `frame`, `lock`, `lost`, `jump`, `end`, `error`; `on(event)` alone is a decorator |
| `frames()` / `events()` | Async iterators of frames and of `lock` / `lost` / `jump` / `end` (raises if the attached input fails) |
| `wait_for(timecode)` | A future that completes when playback passes the timecode: `await` it or call `.result()` |
| `position_at(timestamp)` / `frames_at(timestamp)` | The timecode (or fractional frame count) at any instant, from a fit of the recent signal |
| `frame_rate` / `detected_rate` | The configured rate / the rate in use |
| `offset_frames` | Latency compensation; can be changed while running |
| `lost_after` | Seconds without a frame before `lost` (option, default 0.3) |
| `locked` / `current` / `attached` / `closed` | State |

LTC only: the `channel` option and `level`. MTC only: full-frame locates
arrive as `jump`.

## Listening

The events are available in three styles. Use whichever fits.

### 1. Callbacks

```python
rx.on("frame", lambda tc: ...)     # every frame while locked
rx.on("lock", lambda info: ...)    # LockInfo(timecode, frame_rate, channel)
rx.on("lost", lambda: ...)         # signal stopped or unreadable
rx.on("jump", lambda tc: ...)      # timecode jumped (relocate)
rx.on("end", lambda: ...)          # the attached input finished
rx.on("error", lambda exc: ...)    # the attached input raised
rx.on("lost", handler, once=True)
rx.off("frame", handler)

@rx.on("frame")
def show(tc):
    print(tc)
```

Callbacks run on the thread that delivered the input: the caller of
`push()`, the thread or task reading an attached source, or the receiver's
loss timer (for `lost` when input stops arriving). Keep them short, or hand
the work to another thread. An exception in a callback is logged (logger
`timecode_tools`) and does not stop the others.

### 2. Async iterators

```python
async for tc in rx.frames():
    display(str(tc))

async for ev in rx.events():
    if ev.type == "lock":
        print("locked at", ev.timecode, ev.frame_rate)
    elif ev.type == "jump":
        print("jumped to", ev.timecode)
    elif ev.type == "lost":
        print("lost")
    elif ev.type == "end":
        break  # the attached input finished
```

The iterators subscribe as soon as they are created, and leaving the loop
(`break`, `return`, an exception) unsubscribes. They work whichever thread
delivers the input. If your loop is slower than the incoming frames,
`frames()` drops older unread frames and gives you the newest one; to
receive everything, use `rx.frames(buffer="all")`. `events()` delivers every
event (`buffer="latest"` keeps only the newest), and raises the exception
if the attached input fails.

### 3. `wait_for(target)`

Completes with the first `Timecode` at or past `target` that directly
continues a frame before it, i.e. when normal playback passes the target
(also across midnight). A jump over or onto the target does not count. The
returned `TimecodeWait` is a `concurrent.futures.Future` that can also be
awaited:

```python
tc = await rx.wait_for("01:00:10:00")
tc = await asyncio.wait_for(rx.wait_for("01:00:10:00"), timeout=60)
tc = rx.wait_for("01:00:10:00").result(timeout=60)  # from a thread
```

It registers at once (so input pushed before you await it counts),
survives `detach()` and fails with `ReceiverClosed` on `close()`.
`.cancel()` stops waiting.

### Events

| Event | Payload | When |
| --- | --- | --- |
| `frame` | `Timecode` | Every frame on the followed timeline, while locked |
| `lock` | `LockInfo(timecode, frame_rate, channel)` | The signal became stable; the first `frame` follows |
| `lost` | none | No frame for `lost_after`, the input stopped or was detached, or (MTC) a full-frame locate |
| `jump` | `Timecode` | The timecode moved discontinuously (confirmed by two contiguous frames), or (MTC) a full-frame locate |
| `end` | none | The attached input finished (after `lost`, if locked) |
| `error` | the exception | The attached input raised |

While locked the receiver always follows (chases) the incoming timecode. A
single corrupt frame is ignored and not reported. A frame lost to a dropout
does not break continuity.

### `position_at(timestamp)` and `frames_at(timestamp)`

The timecode at any instant, not only when a frame arrives: `timestamp` is
in seconds on the same monotonic clock as the chunk timestamps (for example
`time.perf_counter()`). The receiver fits a line through the recent frames
and interpolates or extrapolates, so the result is free of the jitter of
your callbacks. `offset_frames` is applied.

```python
now = time.perf_counter()
rx.position_at(now)  # Timecode (its timestamp is `now`), or None
rx.frames_at(now)    # e.g. 90048.37: frames since 00:00:00:00, not wrapped at midnight
```

Both return `None` until the signal has been locked long enough to fit,
after it is lost, and after `close()`.

## Frame rates

| Name | Value | Rate |
| --- | --- | --- |
| `FrameRate.AUTO` | `"auto"` | From the source (default) |
| `FrameRate.FPS_24` | `"24"` | 24 (23.976 reads the same) |
| `FrameRate.FPS_25` | `"25"` | 25 |
| `FrameRate.FPS_2997_DROP` | `"29.97df"` | 29.97 drop-frame (`01:00:00;00`) |
| `FrameRate.FPS_30` | `"30"` | 30 (29.97 non-drop reads the same) |

`FrameRate` is a `str` enum: wherever a rate is taken, the value works too
(`frame_rate="25"`).

LTC and MTC carry the drop-frame flag but not the difference between 24 and
23.976 or between 30 and 29.97 non-drop: their labels are identical and only
the real-time speed differs.

`offset_frames` is added to every timecode. Use a positive value when the
signal reaches you late (the usual case): the receiver then reports the
later timecode that is current by the time you act on it.

## Timecode

```python
from timecode_tools import FrameRate, Timecode

tc = Timecode.parse("01:00:00;00")                 # ';' means drop-frame
tc.frame_index(FrameRate.FPS_2997_DROP)           # 107892
str(Timecode.from_frame_index(-1, FrameRate.FPS_25))  # '23:59:59:24'
tc.compare(Timecode.parse("00:59:59;29"))          # 1
Timecode.parse("00:59:59;29") < tc                 # True
```

A `Timecode` is a frozen dataclass with `hours`, `minutes`, `seconds`,
`frames`, `drop_frame`, `frame_rate` and, when it comes from a receiver,
`timestamp` (s when it began, in the chunks' timestamp clock). `==` compares
the digits and the drop-frame flag; `<`, `>` and `compare()` order by the
digits. `parse()` raises `ValueError` for text that is not a valid
timecode.

## Low-level decoder

`LtcDecoder` has no events, threads or timers: push samples, get the frames
completed in that block (locked or not). It suits code that already
receives audio some other way.

```python
from timecode_tools import LtcDecoder

dec = LtcDecoder(sample_rate=48000)  # channels, channel, frame_rate, offset_frames
frames = dec.push(samples)           # optional 2nd argument: timestamp (s)
# [DecodedFrame(timecode, locked, frame_rate, sample_index), ...]
```

## Command-line example

[`examples/timecode_cli.py`](https://github.com/ShowBuildLaboratories/timecode-tools-python/blob/main/examples/timecode_cli.py)
shows incoming LTC or MTC on one updating terminal line. It is not part of
the package; run it from a checkout of this repository, with
`timecode-tools` installed:

```sh
# LTC from a WAV file (16-bit, 24-bit or 32-bit float), in real time
python examples/timecode_cli.py --file ltc.wav

# LTC from an audio interface through ffmpeg (macOS), channel 2 of 2
ffmpeg -f avfoundation -i ":0" -ac 2 -ar 48000 -f s16le - \
  | python examples/timecode_cli.py --rate 48000 --channels 2 --channel 2

# MTC from a MIDI input (needs: pip install mido python-rtmidi)
python examples/timecode_cli.py --list-midi
python examples/timecode_cli.py --mtc --midi "IAC Driver Bus 1"

# Fixed frame rate and 2 frames of latency compensation
python examples/timecode_cli.py --file ltc.wav --fps 2997df --offset 2
```

Output, one line per event (the clock time is left out here):

```text
lock  00:59:58;03  29.97DF fps  channel 1
00:59:58;03  29.97DF  LOCKED     -4 dBFS
jump  10:00:00;01
lost
```

`python examples/timecode_cli.py --help` lists every option.

## Differences from the Node.js package

The behaviour and the results are the same; the API follows Python
conventions:

- Names are `snake_case` (`wait_for`, `offset_frames`, `FrameRate.FPS_25`).
- Times (chunk timestamps, `Timecode.timestamp`, `lost_after`,
  `position_at`) are in **seconds**, not milliseconds.
- Callbacks with `on()` replace EventEmitter and `addEventListener`;
  `wait_for()` returns a future instead of a promise.
- `events()` delivers every event by default.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for building from source, the golden
files and the release steps.

## License

MIT, Copyright (c) 2026 Show Build Laboratories.
