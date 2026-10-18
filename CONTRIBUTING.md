# Contributing

This repository is the Python package `timecode-tools`. The decoding is done
by the Rust crate
[`timecode-tools`](https://github.com/ShowBuildLaboratories/timecode-tools-rs),
which lives in its own repository together with the shared golden files.
The native module is built with [PyO3](https://pyo3.rs) and
[maturin](https://www.maturin.rs).

## Building

You need Rust (1.88 or later) and Python 3.10 or later.

`Cargo.toml` uses the published crate, pinned to the exact version
(`timecode-tools = "=0.1.0"`). To also compare `goldens/` with the core,
check out the core repository **next to this one**, at the release tag
that matches that version:

```text
some-folder/
  timecode-tools-rs/       https://github.com/ShowBuildLaboratories/timecode-tools-rs
  timecode-tools-python/   this repository
```

```sh
git clone --branch v0.1.0 https://github.com/ShowBuildLaboratories/timecode-tools-rs
git clone https://github.com/ShowBuildLaboratories/timecode-tools-python
cd timecode-tools-python
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest numpy ruff mypy
maturin develop --release    # builds the native module into the venv (omit --release for a debug build)
pytest
python scripts/goldens.py check
ruff check python tests scripts examples && ruff format --check python tests scripts examples
MYPYPATH=python mypy -p timecode_tools
cargo fmt --check && cargo clippy --all-targets -- -D warnings
```

Run `maturin develop` again after changing `src/lib.rs`. The Python code in
`python/timecode_tools/` is installed in editable mode, so changes there
apply at once. `python/timecode_tools/_native.pyi` describes the native
module for type checkers; keep it in step with `src/lib.rs`.

## Golden files

`goldens/` is an exact copy of `goldens/` in the core repository: inputs
and the expected results produced by the reference implementation.
`tests/test_goldens.py` replays the receiver goldens through the public
Python API (once read on a thread, once by an asyncio task) and requires
the same frames, events, `wait_for()` results and final state.

- `python scripts/goldens.py sync` copies the core's `goldens/` here and
  rewrites `goldens/SHA256SUMS`. Run it after the core's goldens change, and
  commit the result.
- `python scripts/goldens.py check` verifies `goldens/` against
  `SHA256SUMS` and, when the core checkout is present, byte for byte against
  the core. CI runs it with `--require-core`.

Set `TIMECODE_TOOLS_RS_DIR` if the core checkout is somewhere other than
`../timecode-tools-rs`. Do not edit files in `goldens/` by hand.

## Updating the core

For a new core release `X.Y.Z` (on crates.io, tagged `vX.Y.Z` in the core
repository):

1. In `Cargo.toml`, change `timecode-tools = "=0.1.0"` to `"=X.Y.Z"` and
   run `cargo update -p timecode-tools`.
2. Check out the core at `vX.Y.Z` next to this repository and run
   `python scripts/goldens.py sync`.
3. Set `CORE_REF: vX.Y.Z` in `.github/workflows/ci.yml` (CI compares
   `goldens/` with that tag).
4. `maturin develop && pytest`, then commit `Cargo.toml`, `Cargo.lock`,
   `goldens/` and the workflow.

To try unreleased core changes locally, add a `[patch]` section to
`.cargo/config.toml` (not committed):

```toml
[patch.crates-io]
timecode-tools = { path = "../timecode-tools-rs/crates/timecode-tools" }
```

## Releasing

The package version follows the Rust crate's version.

1. Update `version` in `pyproject.toml` and in `Cargo.toml` (then
   `cargo update -p timecode-tools-python`), and move the `Unreleased`
   notes in `CHANGELOG.md` under the new version.
2. Check locally: `pytest`, `python scripts/goldens.py check`, the linters
   above, and `maturin sdist` / `maturin build --release`.
3. Push a tag `vX.Y.Z`. The `Release` workflow builds the 9 platform wheels
   (abi3: one per platform for every Python from 3.10) and the sdist, tests
   the wheels on 8 platforms, checks that all are present and that the tag
   matches `pyproject.toml` (the `python-package` artifact), and then waits
   for approval on the `release` environment before uploading to PyPI.
   Publishing uses PyPI trusted publishing (no token); attestations are
   added automatically.
4. Optionally download the `python-package` artifact and try a wheel
   before approving.

## Pull requests

Keep the behaviour the same as the Node.js package
[`timecode-tools`](https://github.com/ShowBuildLaboratories/timecode-tools-node)
and the Dart package
[`timecode_tools`](https://pub.dev/packages/timecode_tools), with names in
Python style (see "Differences from the Node.js package" in the README).
Add tests for behaviour changes.
