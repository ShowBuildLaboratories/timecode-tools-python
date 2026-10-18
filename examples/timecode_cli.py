#!/usr/bin/env python3
"""timecode_cli: shows incoming LTC or MTC on one updating terminal line.

    python examples/timecode_cli.py --file ltc.wav
    ffmpeg -f avfoundation -i ":0" -ac 1 -ar 48000 -f s16le - | python examples/timecode_cli.py --rate 48000
    python examples/timecode_cli.py --mtc --midi "IAC Driver Bus 1"

It is also a sample of the API: every input becomes an iterable of chunks
handed to LtcReceiver/MtcReceiver.attach() (read on a thread), or, for a
MIDI port, bytes pushed from the MIDI library's callback with push(). The
display follows the receiver's callbacks.
"""

from __future__ import annotations

import argparse
import array
import math
import struct
import sys
import threading
import time
from collections.abc import Iterator

from timecode_tools import FrameRate, LtcReceiver, MidiChunk, MtcReceiver, PcmChunk

USAGE = """\
  timecode_cli.py --file <ltc.wav> [options]
  <pcm source> | timecode_cli.py --rate <hz> [--format s16le|f32le] [--channels <n>] [options]
  timecode_cli.py --mtc --midi <port> [options]
  <midi bytes> | timecode_cli.py --mtc [options]
  timecode_cli.py --list-midi

--midi and --list-midi need the optional packages mido and python-rtmidi:
  pip install mido python-rtmidi"""

FPS = {
    "auto": FrameRate.AUTO,
    "24": FrameRate.FPS_24,
    "25": FrameRate.FPS_25,
    "2997df": FrameRate.FPS_2997_DROP,
    "30": FrameRate.FPS_30,
}
RATE_LABEL = {"24": "24", "25": "25", "29.97df": "29.97DF", "30": "30"}


def read_wav(path: str) -> tuple[int, int, array.array]:
    """(sample rate, channels, interleaved samples) of a 16-bit, 24-bit or
    32-bit float PCM WAV file. 16-bit gives int16 samples, the others float."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt = None
    at = 12
    while at + 8 <= len(data):
        tag, size = data[at : at + 4], struct.unpack_from("<I", data, at + 4)[0]
        body = at + 8
        if tag == b"fmt ":
            code, channels, rate = struct.unpack_from("<HHI", data, body)
            bits = struct.unpack_from("<H", data, body + 14)[0]
            if code == 0xFFFE:  # extensible
                code = struct.unpack_from("<H", data, body + 24)[0]
            fmt = (code, channels, rate, bits)
        elif tag == b"data":
            if fmt is None:
                raise ValueError("WAV data before fmt chunk")
            code, channels, rate, bits = fmt
            pcm = data[body : min(body + size, len(data))]
            return rate, channels, _decode(pcm, code, bits)
        at = body + size + (size & 1)
    raise ValueError("WAV file has no data chunk")


def _decode(pcm: bytes, code: int, bits: int) -> array.array:
    if code == 1 and bits == 16:
        out = array.array("h", pcm[: len(pcm) & ~1])
    elif code == 1 and bits == 24:
        n = len(pcm) // 3
        out = array.array(
            "f", (int.from_bytes(pcm[i * 3 : i * 3 + 3], "little", signed=True) / 8388608 for i in range(n))
        )
        return out
    elif code == 3 and bits == 32:
        out = array.array("f", pcm[: len(pcm) & ~3])
    else:
        raise ValueError(f"unsupported WAV format (code {code}, {bits}-bit)")
    if sys.byteorder == "big":
        out.byteswap()
    return out


def file_chunks(path: str, fast: bool) -> Iterator[PcmChunk]:
    """WAV file -> 10 ms chunks, paced in real time unless fast."""
    rate, channels, samples = read_wav(path)
    block = round(rate / 100) * channels
    start = time.perf_counter()
    for i in range(0, len(samples), block):
        at = i / channels / rate
        if not fast:
            wait = start + at - time.perf_counter()
            if wait > 0.001:
                time.sleep(wait)
        yield PcmChunk(samples[i : i + block], rate, channels, start + at)


def stdin_chunks(rate: int, channels: int, fmt: str) -> Iterator[PcmChunk]:
    """Raw PCM on standard input -> chunks. The arrival time stands in for
    the capture time, minus the duration of the block."""
    frame_bytes = (4 if fmt == "f32le" else 2) * channels
    rest = b""
    stdin = sys.stdin.buffer
    while True:
        data = stdin.read1(65536)
        if not data:
            return
        now = time.perf_counter()
        buffer = rest + data
        usable = len(buffer) - len(buffer) % frame_bytes
        rest = buffer[usable:]
        if usable == 0:
            continue
        samples = array.array("f" if fmt == "f32le" else "h", buffer[:usable])
        if sys.byteorder == "big":
            samples.byteswap()
        yield PcmChunk(samples, rate, channels, now - len(samples) / channels / rate)


def stdin_midi() -> Iterator[MidiChunk]:
    """Raw MIDI bytes on standard input, timestamped on arrival."""
    stdin = sys.stdin.buffer
    while True:
        data = stdin.read1(4096)
        if not data:
            return
        yield MidiChunk(data, time.perf_counter())


def load_mido():  # type: ignore[no-untyped-def]
    try:
        import mido
    except ImportError:
        fail("--midi and --list-midi need mido and python-rtmidi: pip install mido python-rtmidi")
    return mido


def fail(message: str) -> None:
    sys.stderr.write(f"timecode_cli: {message}\n\nUsage:\n{USAGE}\n")
    sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="timecode_cli.py", usage=USAGE, add_help=True)
    p.add_argument("--file", help="read a WAV file (16-bit, 24-bit or 32-bit float PCM)")
    p.add_argument("--fast", action="store_true", help="with --file: read as fast as possible, not in real time")
    p.add_argument("--rate", type=int, help="sample rate of raw PCM on standard input")
    p.add_argument("--format", default="s16le", choices=["s16le", "f32le"], help="raw PCM format on standard input")
    p.add_argument("--channels", type=int, help="interleaved channels in the input (default 1, or the WAV's)")
    p.add_argument("--channel", type=int, default=1, help="channel that carries LTC (1-based, default 1)")
    p.add_argument("--mtc", action="store_true", help="read MIDI Time Code instead of LTC")
    p.add_argument("--midi", help="MIDI input port, by name (or part of it) or number")
    p.add_argument("--list-midi", action="store_true", help="list MIDI input ports")
    p.add_argument("--fps", default="auto", choices=list(FPS), help="frame rate (default auto)")
    p.add_argument("--offset", type=int, default=0, help="add this many frames (latency correction)")
    args = p.parse_args(argv)

    if args.list_midi:
        ports = load_mido().get_input_names()
        print("\n".join(f"{i}: {name}" for i, name in enumerate(ports)) if ports else "no MIDI inputs")
        return 0

    rx: LtcReceiver | MtcReceiver
    if args.mtc:
        if args.file or args.rate:
            fail("--mtc reads MIDI, not audio: use --midi or standard input")
        rx = MtcReceiver(frame_rate=FPS[args.fps], offset_frames=args.offset)
    else:
        if args.midi:
            fail("--midi needs --mtc")
        if not 1 <= args.channel <= 64:
            fail("--channel must be 1..64")
        rx = LtcReceiver(frame_rate=FPS[args.fps], offset_frames=args.offset, channel=args.channel)

    tty = sys.stdout.isatty()
    out = threading.Lock()
    state = {"current": None, "searching": False, "last_second": None}
    ended = threading.Event()

    def dbfs() -> str:
        if isinstance(rx, MtcReceiver):
            return "MTC"
        level = rx.level
        return f"{round(20 * math.log10(level))} dBFS" if level > 0 else "-inf dBFS"

    def status_line() -> str:
        locked = rx.locked
        current = state["current"]
        tc = str(current) if current and locked else "--:--:--:--"
        rate = RATE_LABEL[current.frame_rate.value] if current and locked and current.frame_rate else "--"
        shown = "LOCKED" if locked else "SEARCHING" if state["searching"] else "NO SIGNAL"
        return f"{tc}  {rate:<7}  {shown:<9}  {dbfs()}"

    def render() -> None:
        if tty:
            with out:
                sys.stdout.write(f"\r{status_line()}\x1b[K")
                sys.stdout.flush()

    def log(text: str) -> None:
        t = time.time()
        clock = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
        with out:
            sys.stdout.write(f"\r\x1b[K{clock}  {text}\n" if tty else f"{clock}  {text}\n")
            sys.stdout.flush()
        render()

    def on_lock(info) -> None:  # type: ignore[no-untyped-def]
        channel = "" if isinstance(rx, MtcReceiver) else f"  channel {info.channel}"
        log(f"lock  {info.timecode}  {RATE_LABEL[info.frame_rate.value]} fps{channel}")

    def on_frame(tc) -> None:  # type: ignore[no-untyped-def]
        state["current"] = tc
        render()
        # Without a terminal (piped to a file), print one line per second.
        second = str(tc)[:8]
        if not tty and second != state["last_second"]:
            with out:
                sys.stdout.write(f"{status_line()}\n")
        state["last_second"] = second

    def on_lost() -> None:
        state["current"] = None
        log("lost")

    def on_error(error: BaseException) -> None:
        with out:
            sys.stderr.write(f"\ntimecode_cli: {error}\n")
        state["error"] = True

    rx.on("lock", on_lock)
    rx.on("frame", on_frame)
    rx.on("jump", lambda tc: log(f"jump  {tc}"))
    rx.on("lost", on_lost)
    rx.on("error", on_error)
    rx.on("end", ended.set)

    port = None
    if args.mtc and args.midi:
        mido = load_mido()
        ports = mido.get_input_names()
        name = None
        if args.midi.isdigit() and int(args.midi) < len(ports):
            name = ports[int(args.midi)]
        else:
            name = next((p for p in ports if args.midi in p), None)
        if name is None:
            fail(f"no MIDI input matches {args.midi!r}. Inputs: {', '.join(ports) or '(none)'}")
        # The callback runs on the MIDI library's thread; push() is thread-safe.
        port = mido.open_input(name, callback=lambda msg: rx.push(MidiChunk(msg.bytes(), time.perf_counter())))
        sys.stderr.write(f"reading MTC from {name}\n")
    elif args.mtc:
        if sys.stdin.isatty():
            fail("no input: use --midi <port>, or pipe raw MIDI bytes into standard input")
        rx.attach(stdin_midi())
    elif args.file:
        if args.rate:
            fail("--rate is for standard input; a WAV file has its own")
        try:
            chunks = file_chunks(args.file, args.fast)
            first = next(chunks)  # reads the file now, so errors show up here
        except (OSError, ValueError) as e:
            fail(str(e))

        def all_chunks() -> Iterator[PcmChunk]:
            yield first
            yield from chunks

        rx.attach(all_chunks())
    else:
        if sys.stdin.isatty():
            fail("no input: use --file, or pipe raw PCM into standard input")
        if not args.rate:
            fail("--rate is required for standard input")
        rx.attach(stdin_chunks(args.rate, args.channels or 1, args.format))

    try:
        # Repaint the level and "SEARCHING" state even without frames.
        while not ended.wait(0.1):
            if not rx.locked:
                state["searching"] = isinstance(rx, LtcReceiver) and rx.level > 0.02
            render()
    except KeyboardInterrupt:
        rx.close()
        if port is not None:
            port.close()
        if tty:
            sys.stdout.write("\n")
        return 130
    with out:
        sys.stdout.write(f"\r{status_line()}\x1b[K\n" if tty else f"{status_line()}\n")
    rx.close()
    return 1 if state.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
