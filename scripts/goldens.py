#!/usr/bin/env python3
"""Keeps goldens/ an exact copy of the golden files in the Rust core
repository (https://github.com/ShowBuildLaboratories/timecode-tools-rs,
folder goldens/).

    python scripts/goldens.py sync    copy the core's goldens/ here and write SHA256SUMS
    python scripts/goldens.py check   verify goldens/ against SHA256SUMS and, when the
                                      core checkout is present, against the core

The core checkout is looked for in $TIMECODE_TOOLS_RS_DIR, else
../timecode-tools-rs. `check --require-core` fails when it is missing.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "goldens"
SUMS = "SHA256SUMS"
# Files in goldens/ that this repository adds itself.
OWN = {SUMS}


def core_dir() -> Path:
    return Path(os.environ.get("TIMECODE_TOOLS_RS_DIR") or ROOT.parent / "timecode-tools-rs").resolve()


def list_files(directory: Path, skip: set = frozenset()) -> list[str]:
    """Relative paths (with '/') of every file under directory, sorted."""
    out = []
    for path in directory.rglob("*"):
        rel = path.relative_to(directory).as_posix()
        if path.is_file() and path.name != ".DS_Store" and rel not in skip:
            out.append(rel)
    return sorted(out)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sums() -> dict[str, str] | None:
    file = LOCAL / SUMS
    if not file.exists():
        return None
    sums = {}
    for line in file.read_text().splitlines():
        m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
        if m:
            sums[m.group(2)] = m.group(1)
    return sums


def check_sums() -> list[str]:
    """Differences between goldens/ and its SHA256SUMS. Empty when they agree."""
    if not LOCAL.exists():
        return ["goldens/ is missing"]
    sums = read_sums()
    if sums is None:
        return [f"goldens/{SUMS} is missing"]
    problems = []
    files = list_files(LOCAL, OWN)
    for f in files:
        if f not in sums:
            problems.append(f"goldens/{f} is not listed in {SUMS}")
        elif sums[f] != sha256(LOCAL / f):
            problems.append(f"goldens/{f} differs from {SUMS}")
    for f in sums:
        if f not in files:
            problems.append(f"goldens/{f} is listed but missing")
    if not sums:
        problems.append(f"{SUMS} lists no files")
    return problems


def check_core(directory: Path | None = None) -> list[str] | None:
    """Differences between goldens/ and the core's goldens/. None when the
    core checkout is not present."""
    source = (directory or core_dir()) / "goldens"
    if not source.exists():
        return None
    problems = []
    want = list_files(source)
    have = list_files(LOCAL, OWN)
    for f in want:
        if f not in have:
            problems.append(f"goldens/{f} is missing (present in the core)")
        elif (source / f).read_bytes() != (LOCAL / f).read_bytes():
            problems.append(f"goldens/{f} differs from the core")
    for f in have:
        if f not in want:
            problems.append(f"goldens/{f} is not in the core")
    return problems


def sync(directory: Path | None = None) -> int:
    directory = directory or core_dir()
    source = directory / "goldens"
    if not source.exists():
        raise SystemExit(f"no goldens/ in {directory}; set TIMECODE_TOOLS_RS_DIR")
    shutil.rmtree(LOCAL, ignore_errors=True)
    files = list_files(source)
    for f in files:
        (LOCAL / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / f, LOCAL / f)
    lines = [f"{sha256(LOCAL / f)}  {f}" for f in files]
    (LOCAL / SUMS).write_bytes(("\n".join(lines) + "\n").encode())
    return len(files)


def main(argv: list[str]) -> int:
    command, flags = (argv[0] if argv else None), argv[1:]
    if command == "sync":
        print(f"copied {sync()} files from {core_dir() / 'goldens'}")
        return 0
    if command == "check":
        problems = check_sums()
        core = check_core()
        if core is None:
            msg = f"no goldens/ in the core checkout at {core_dir()}"
            if "--require-core" in flags:
                problems.append(msg)
            else:
                print(f"{msg}; checked goldens/ against {SUMS} only")
        else:
            problems.extend(core)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print("goldens/ is up to date")
        return 0
    print("usage: goldens.py sync | check [--require-core]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
