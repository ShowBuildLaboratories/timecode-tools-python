"""Replays the shared receiver golden files (goldens/, copied from the Rust
core repository) through the public Python API: the same frames (timecode,
and timestamp within 3 us), the same lock / lost / jump events and
frames_at() value (within 1e-6 frames) after each input chunk, the same
wait_for() results and the same final state.

Each golden runs twice: input attached as a plain iterable (read on a
thread) and as an async iterable (read by an asyncio task)."""

from __future__ import annotations

import array
import asyncio
import importlib.util
import json
import shutil
import threading
from pathlib import Path

import pytest
from helpers import js_round, reference_ltc
from timecode_tools import LtcReceiver, MidiChunk, MtcReceiver, PcmChunk

ROOT = Path(__file__).resolve().parent.parent
NOMINAL = {"24": 24, "25": 25, "29.97df": 30, "30": 30}

spec = importlib.util.spec_from_file_location("goldens", ROOT / "scripts" / "goldens.py")
goldens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(goldens)


def test_goldens_match_sha256sums():
    assert goldens.check_sums() == []


def test_goldens_are_an_exact_copy_of_the_core():
    problems = goldens.check_core()
    if problems is None:
        pytest.skip(f"no goldens/ in the core checkout at {goldens.core_dir()}")
    assert problems == []


def test_copy_check_reports_changed_missing_and_extra_files(tmp_path):
    shutil.copytree(goldens.LOCAL, tmp_path / "goldens")
    (tmp_path / "goldens" / "SHA256SUMS").unlink()
    assert goldens.check_core(tmp_path) == []
    files = goldens.list_files(tmp_path / "goldens")
    with open(tmp_path / "goldens" / files[0], "ab") as f:
        f.write(b" ")
    (tmp_path / "goldens" / files[1]).unlink()
    (tmp_path / "goldens" / "new.json").write_text("{}")
    assert sorted(goldens.check_core(tmp_path)) == sorted(
        [
            f"goldens/{files[0]} differs from the core",
            f"goldens/{files[1]} is not in the core",
            "goldens/new.json is missing (present in the core)",
        ]
    )


def receiver_goldens():
    out = []
    for path in sorted(goldens.LOCAL.rglob("*.json")):
        data = json.loads(path.read_text())
        if data.get("kind") in ("ltc", "mtc") and isinstance(data.get("steps"), list):
            out.append(data)
    return sorted(out, key=lambda g: g["name"])


CASES = receiver_goldens()


def test_receiver_goldens_are_present():
    assert len(CASES) >= 7


def make_receiver(golden):
    o = golden["options"]
    kwargs = {"offset_frames": o.get("offset_frames") or 0}
    if o.get("frame_rate"):
        kwargs["frame_rate"] = o["frame_rate"].lower()
    if golden["kind"] == "ltc":
        if o.get("channel"):
            kwargs["channel"] = o["channel"]
        return LtcReceiver(**kwargs)
    return MtcReceiver(**kwargs)


def ltc_chunks(i):
    rate = i["rate"].lower()
    mono = array.array("h")
    for seg in i["segments"]:
        if seg.get("silence") is not None:
            mono.extend(array.array("h", bytes(2 * seg["silence"])))
        else:
            mono.extend(reference_ltc(i["sample_rate"], rate, NOMINAL[rate], seg["start"], seg["frame_count"]))
    channels = i["channels"]
    pcm = mono
    if channels > 1:
        pcm = array.array("h", bytes(2 * len(mono) * channels))
        for n, v in enumerate(mono):
            for c in range(1, channels + 1):
                pcm[n * channels + c - 1] = v if c == i["ltc_channel"] else (n * 37) % 4000 - 2000
    assert len(pcm) == i["sample_count"], "LTC input length"
    per = i["chunk"] * channels
    out = []
    for at in range(0, len(pcm), per):
        first = at // channels
        us = i["t0_us"] + js_round(first * 1e6 / i["sample_rate"])
        out.append(PcmChunk(pcm[at : at + per], i["sample_rate"], channels, us / 1e6))
    return out


def mtc_chunks(i):
    return [MidiChunk(bytes.fromhex(c["bytes"]), c["t_us"] / 1e6) for c in i["chunks"]]


def check_timestamp(tc, want_us, context):
    if want_us is None:
        assert tc.timestamp is None, context
    else:
        assert abs(tc.timestamp * 1e6 - want_us) <= 3, f"{context}: timestamp {tc.timestamp} s vs {want_us} us"


@pytest.mark.parametrize("mode", ["thread", "asyncio"])
@pytest.mark.parametrize("golden", CASES, ids=[g["name"] for g in CASES])
def test_golden(golden, mode):
    name = golden["name"]
    rx = make_receiver(golden)
    chunks = ltc_chunks(golden["input"]) if golden["kind"] == "ltc" else mtc_chunks(golden["input"])

    steps = []
    chunk_of = {}

    def on_frame(tc):
        steps[-1]["frames"].append(tc)
        chunk_of[id(tc)] = len(steps) - 1

    rx.on("frame", on_frame)
    for kind in ("lock", "lost", "jump"):
        rx.on(kind, lambda *_, kind=kind: steps[-1]["events"].append(kind))
    waits = {target: rx.wait_for(target) for target in golden["wait_for"]}

    final = {}

    def source():
        for i, chunk in enumerate(chunks):
            steps.append({"frames": [], "events": []})
            yield chunk
            # Back here once the chunk has been processed.
            at = golden["steps"][i].get("frames_at") if i < len(golden["steps"]) else None
            if at:
                steps[-1]["frames_at"] = rx.frames_at(at["t_us"] / 1e6)
        # State after the last chunk, before end-of-input reports 'lost'.
        final.update(locked=rx.locked, rate=rx.detected_rate, current=str(rx.current) if rx.current else None)
        steps.append({"frames": [], "events": [], "after_input": True})

    if mode == "thread":
        ended = threading.Event()
        rx.on("end", ended.set)
        rx.attach(source())
        assert ended.wait(60), f"{name}: input did not end"
    else:

        async def asource():
            for chunk in source():
                yield chunk

        async def run():
            ended = asyncio.Event()
            rx.on("end", ended.set)
            rx.attach(asource())
            await asyncio.wait_for(ended.wait(), 60)

        asyncio.run(run())

    after = steps.pop()
    assert after.get("after_input")
    assert after["events"] == (["lost"] if golden["final_locked"] else []), f"{name}: end of input"
    assert len(steps) == len(golden["steps"]), f"{name}: chunk count"
    for i, (want, got) in enumerate(zip(golden["steps"], steps, strict=False)):
        assert got["events"] == want["events"], f"{name} chunk {i}: events"
        if want.get("frames_at"):
            w, g = want["frames_at"]["value"], got.get("frames_at")
            if w is None:
                assert g is None, f"{name} chunk {i}: frames_at"
            else:
                assert g is not None and abs(g - w) < 1e-6, f"{name} chunk {i}: frames_at {g} vs {w}"
        assert len(got["frames"]) == len(want["frames"]), f"{name} chunk {i}: frame count"
        for tc, w in zip(got["frames"], want["frames"], strict=False):
            assert str(tc) == w["tc"], f"{name} chunk {i}"
            check_timestamp(tc, w["ts_us"], f"{name} chunk {i} {tc}")
    for target, want in golden["wait_for"].items():
        fut = waits[target]
        if want is None:
            assert not fut.done(), f"{name} wait_for {target}"
            continue
        assert fut.done(), f"{name} wait_for {target}: not resolved"
        got = fut.result()
        assert chunk_of[id(got)] == want["chunk"], f"{name} wait_for {target}: chunk"
        assert str(got) == want["tc"], f"{name} wait_for {target}"
        check_timestamp(got, want["ts_us"], f"{name} wait_for {target}")
    assert final["locked"] == golden["final_locked"], f"{name}: locked"
    want_rate = golden["final_detected_rate"].lower() if golden["final_detected_rate"] else None
    assert final["rate"] == want_rate, f"{name}: rate"
    assert final["current"] == golden["final_current"], f"{name}: current"
    rx.close()
