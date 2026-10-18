# Changelog

All notable changes to the Python package `timecode-tools` are documented
here. Its version follows the Rust crate
[`timecode-tools`](https://crates.io/crates/timecode-tools) it is built on.
This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

## 0.1.0

First public release.

- `LtcReceiver` (PCM chunks) and `MtcReceiver` (MIDI bytes), sharing the
  `TimecodeReceiver` interface: `push()` from any thread, `attach()` an
  iterable (read on a thread) or async iterable (read by an asyncio task),
  `detach()` / `close()`, events `frame`, `lock`, `lost`, `jump`, `end` and
  `error` through callbacks (`on` / `off`) or async iterators (`frames()`,
  `events()`), `wait_for(timecode)` (a future, awaitable),
  `position_at(timestamp)` and `frames_at(timestamp)`, `frame_rate`,
  `detected_rate`, `offset_frames`, `lost_after`, `closed`.
- `LtcDecoder`: low-level `push()` returning decoded frames.
- `Timecode` parsing, formatting, comparison and frame arithmetic;
  `FrameRate`.
- Samples from numpy arrays (1-D or 2-D), `array.array`, `memoryview` or
  raw 16-bit little-endian bytes; int16, float32 (and float64, converted).
- Type hints (`py.typed`).
- abi3 wheels for Python 3.10 and later on macOS (arm64, x64), Windows
  (x64, arm64) and Linux (x64, arm64, armv7; glibc and musl).
- Results checked against the shared golden files of the Rust core.
- `examples/timecode_cli.py`: terminal LTC and MTC reader for WAV files, raw
  PCM or MIDI bytes on standard input, and MIDI inputs.
