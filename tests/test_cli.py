"""Runs the examples/timecode_cli.py sample on generated LTC and MTC input."""

from __future__ import annotations

import array
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

from timecode_tools._testing import generate_ltc, generate_mtc

CLI = Path(__file__).resolve().parent.parent / "examples" / "timecode_cli.py"


def write_wav(path: Path, samples: array.array, sample_rate: int, channels: int = 1) -> None:
    """16-bit PCM WAV from interleaved int16 samples."""
    data = samples.tobytes() if sys.byteorder == "little" else _swapped(samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * channels * 2, channels * 2, 16)
    path.write_bytes(header + b"data" + struct.pack("<I", len(data)) + data)


def _swapped(samples: array.array) -> bytes:
    a = array.array("h", samples)
    a.byteswap()
    return a.tobytes()


def lines_of(out: str) -> list[str]:
    return [re.sub(r"^\d\d:\d\d:\d\d\.\d{3}  ", "", line) for line in out.strip().split("\n")]


def test_reads_a_wav_file(tmp_path):
    frames = round(6 * 30000 / 1001)
    pcm = generate_ltc(frame_rate="29.97df", start="00:59:58;00", frame_count=frames // 2)
    pcm.extend(generate_ltc(frame_rate="29.97df", start="10:00:00;00", frame_count=frames - frames // 2))
    wav = tmp_path / "ltc.wav"
    write_wav(wav, pcm, 48000)
    out = subprocess.run(
        [sys.executable, str(CLI), "--file", str(wav), "--fast"], capture_output=True, text=True, check=True
    ).stdout
    lines = lines_of(out)
    assert lines[:-1] == [
        "lock  00:59:58;03  29.97DF fps  channel 1",
        "00:59:58;03  29.97DF  LOCKED     -4 dBFS",
        "00:59:59;00  29.97DF  LOCKED     -4 dBFS",
        "01:00:00;00  29.97DF  LOCKED     -4 dBFS",
        "jump  10:00:00;01",
        "10:00:00;01  29.97DF  LOCKED     -4 dBFS",
        "10:00:01;00  29.97DF  LOCKED     -4 dBFS",
        "10:00:02;00  29.97DF  LOCKED     -4 dBFS",
        "lost",
    ]
    assert re.match(r"^--:--:--:--  --\s+(NO SIGNAL|SEARCHING)", lines[-1])


def test_reads_mtc_bytes_from_standard_input():
    child = subprocess.Popen(
        [sys.executable, str(CLI), "--mtc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False
    )
    # Send each quarter frame at its real time (3 seconds in all).
    start = time.perf_counter()
    for data, t in generate_mtc("25", "01:59:59:00", 75):
        wait = start + t - time.perf_counter()
        if wait > 0.001:
            time.sleep(wait)
        child.stdin.write(data)
        child.stdin.flush()
    child.stdin.close()
    out = child.stdout.read().decode().replace("\r\n", "\n")
    assert child.wait(10) == 0
    assert lines_of(out)[:5] == [
        "lock  01:59:59:03  25 fps",
        "01:59:59:03  25       LOCKED     MTC",
        "02:00:00:00  25       LOCKED     MTC",
        "02:00:01:00  25       LOCKED     MTC",
        "lost",
    ]


def test_usage_errors():
    run = subprocess.run([sys.executable, str(CLI), "--mtc", "--file", "x.wav"], capture_output=True, text=True)
    assert run.returncode == 2
    assert "--mtc reads MIDI" in run.stderr
    run = subprocess.run([sys.executable, str(CLI), "--file", "missing.wav"], capture_output=True, text=True)
    assert run.returncode == 2
    assert "missing.wav" in run.stderr
