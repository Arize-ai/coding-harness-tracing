# Hooks binary benchmark (Arize-ai/arize#90274)

Local tooling to build the `arize-harness` hooks as a Nuitka standalone binary and to measure what
that costs per hook against today's venv install. The binary feasibility spike that chose Nuitka is
kept on the tag `spike/binary-packaging-90274`; `reports/` holds its findings. Nothing here changes
hook code, installers or harness configs, and nothing here runs in CI.

```
build/      venv.sh, nuitka.sh, universal.sh, common.sh (pins), inputs.py (what a build must include), entry.py,
            selftest.sh (the build gates' own tests)
timing/     run.sh (drives everything), hooks.py (per-hook timing), first-run.sh, summarize.py,
            harness.py (throwaway HOME + hook runner), lifecycle.py (per-phase timing of one hook)
checks/     equiv.py: same output as venv312, real HTTPS, dry-run span, session key
reports/    the spike's Nuitka correctness checks: CHECKS.md
```

Builds go to `builds/`, results to `results/`, build tools and Nuitka's scratch to `.workbench/` at
the repo root. All three are git-ignored.

## Build

macOS only. Needs [uv](https://docs.astral.sh/uv/) 0.11.24, the Xcode command line tools and Rosetta
(for the x86_64 half).

```bash
benchmark/build/venv.sh               # builds/venv312: the baseline, a venv with this checkout
ARCH=arm64 benchmark/build/nuitka.sh  # builds/nuitka-arm64        ~20 s
ARCH=x86_64 benchmark/build/nuitka.sh # builds/nuitka-x86_64       ~30 s, re-runs itself under arch -x86_64
benchmark/build/universal.sh          # builds/nuitka-universal    ~10 s on top of both halves: lipo merge, re-sign, verify
```

Every build comes from one interpreter, named exactly in `build/common.sh`: uv's
`cpython-3.12.10-macos-aarch64-none` and `cpython-3.12.10-macos-x86_64-none`, installed with
`uv python install --managed-python`. The build tools (`requirements-build.txt`: nuitka 4.3,
ordered-set 4.1.0, python-dotenv 1.2.4, plus this checkout) go into `.workbench/bt-arm64` and
`.workbench/bt-x86_64`, also created with `--managed-python`. `BUILDTOOLS=<venv>` uses another
venv for `nuitka.sh`, which still has to pass the interpreter check below. `universal.sh` needs
both architectures' venvs, so it stops if `BUILDTOOLS` is set.

**uv version.** The interpreter names pin the Python version but not the python-build-standalone
release behind it; that comes from uv. The numbers in `reports/` and in the PR were taken with uv
0.11.24 (`UV_VERSION` in `build/common.sh`; another uv gets a warning). Use the same uv, or expect
to re-baseline. After a deliberate uv or Python bump, `universal.sh` may stop on the minos check:
check the new values it prints, then either set `EXPECT_MINOS_ARM64` / `EXPECT_MINOS_X86_64` or
update their defaults in `universal.sh`.

**Why not python.org's Python?** It is the only universal2 CPython, so it looks like the easy way
to a universal binary. But its OpenSSL is built with `OPENSSLDIR` inside
`/Library/Frameworks/Python.framework/...`, which is empty until the user runs Install
Certificates, and a frozen build keeps that path, so HTTPS sends would fail with
`CERTIFICATE_VERIFY_FAILED`. uv's interpreters read the system `/etc/ssl/cert.pem`. Nuitka also can't build universal itself
(`--macos-target-arch` takes `arm64` or `x86_64`), so `universal.sh` merges two single-arch builds.

**What a build was made from.** Every successful build writes `results/<label>/build_info.json`:
`source`, a sha256 of `core/`, `tracing/`, `benchmark/build/` (not `selftest.sh`),
`requirements-build.txt` and `pyproject.toml` as they were on disk when the build started (so it
covers uncommitted edits, and README or timing edits don't change it), plus the commit (`-dirty` if
those paths have changes), Python, Nuitka and uv versions. `universal.sh` merges only two halves
with the same `source` as this checkout, so after any change to those files rebuild both halves.
A stopped build has no `build_info.json`, so it can't be merged either. Builds made before this
was recorded have none: rebuild them.

## Time

```bash
benchmark/timing/run.sh           # ~10 min; results/bench-result.json and results/table.md
benchmark/timing/run.sh --quick   # ~1 min: check that everything runs
HOOKS_ONLY=1 benchmark/timing/run.sh  # skip the first runs, keep their earlier results
benchmark/timing/run.sh --results DIR  # everything this run writes in DIR instead of results/
```

`--results DIR` covers first runs, `bench-result.json` and `table.md`. Build records
(`build_seconds`, `build_info.json`) are always read from `results/`, where the builds write them.
With `HOOKS_ONLY=1`, first runs come from DIR too, so a new DIR has none.

`run.sh` times `venv312`, `nuitka-arm64` and `nuitka-universal` (missing builds are skipped):

- **first run** (`first-run.sh`): the first launch of a fresh copy of the build, five copies,
  writable and read-only (like a root-owned MDM install). This is what a user sees once after an
  install or update, before macOS has scanned the new binaries. venv312's number is never a true
  first run, because its interpreter lives outside the venv. Every launch has to exit 0; one that
  doesn't marks the build INVALID, and `run.sh` goes on with the rest.
- **per hook** (`hooks.py`): SessionStart, UserPromptSubmit, PreToolUse, PostToolUse and Stop,
  30 runs each, back to back and after 0.5 s idle, in a throwaway HOME with dry-run on. Builds
  alternate run by run so machine drift hits all of them alike. Before each timed call the untimed
  calls that make it do real work run first (a fresh session, an open turn, the matching Pre).

`summarize.py` turns `bench-result.json` into one column per build. The only judged number is the
budget: back-to-back p95 at most 25 ms slower than venv312 on SessionStart and Stop. Back to back
is the floor; idle is closer to a real session, where every hook follows model or tool time. A
source row shows what each build was made from, with a warning when the builds differ or one has
no record.

`timing/lifecycle.py` splits one hook's wall time into spawn, boot and run.

## Gates

A fast build that does nothing is worthless, so each number has a check in front of it. A failed
build or verification gate stops the script; a failed timing gate marks the build INVALID in the
table instead of PASS/FAIL.

| gate | where | what it checks | protects |
|---|---|---|---|
| interpreter | `common.sh` `check_python`, before every build | Python 3.12.10 for the right arch, and `ssl.get_default_verify_paths().openssl_cafile` is `/etc/ssl/cert.pem` | that the binary can send spans over HTTPS at all, and that every build is timed on the same Python |
| arch | end of `nuitka.sh` | `lipo -archs` of the executable is `$ARCH` | that the x86_64 half really is x86_64 |
| inputs | `inputs.py modules` / `data`: `nuitka.sh` before compiling. `inputs.py check`: end of every build, and `hooks.py` before timing. Data in the bundle: end of the three Nuitka builds only | `inputs.py modules` and `data` succeed (a missing data path stops the build unless it's in `EXPECTED_MISSING`, and the previous bundle is kept); `inputs.py check`: `hook --list` matches the hook table, and every hook loads and exits 0 (a crash or signal fails it); every listed data file is in the bundle and identical to its source | every timing: a binary missing a handler or a data file, or crashing on start, is never timed |
| universal | `universal.sh` | same files in both halves, non-Mach-O files identical, `codesign --verify --strict`, every Mach-O file `x86_64 arm64`, minos `EXPECT_MINOS_ARM64` / `EXPECT_MINOS_X86_64` (default 11.0 / 10.15), inputs check and a dry-run span both natively and under `arch -x86_64` | the universal column: it describes a binary that works on both architectures |
| sources | `universal.sh`, before merging | both halves' `build_info.json` exist and have the same `source` as this checkout | that the universal binary is one program, built from the code being timed |
| spans | `hooks.py` | every Stop, warmups included, logs its dry-run span | the Stop numbers (and, since every timed call is set up the same way, the rest): Stop really builds and sends a trace |
| first runs | `first-run.sh`, read by `hooks.py` | every copy's first launch, writable and read-only, exits 0 within 30 s; a copy that doesn't is left out of the times and listed in `results/<label>/first-run.failed` (`first-run-ro.failed`) | the first-run rows: a build that crashes on its first launch after an install is never reported as fast |

`build/selftest.sh` tests the build gates themselves: in a throwaway copy of the repo it breaks
each one on purpose (failing `inputs.py`, a hook module left out of the build, a missing or changed
data file, a wrong minos, halves from different sources, a source changed after both halves) and
checks that the build stops with the expected message, and that the previous bundle survives a
stopped build. Run it after both single-arch builds: it reuses their bundles and
`.workbench/bt-arm64`.

Beyond the gates, `checks/equiv.py` compares a build with venv312 (run these by hand after a
change to the build; the driver needs this checkout's `core/`, so use a build-tools Python):

```bash
PY=.workbench/bt-arm64/bin/python B=$PWD/benchmark/builds
$PY -I benchmark/checks/equiv.py sequence venv312 $B/venv312/bin/arize-harness /tmp/venv.json
$PY -I benchmark/checks/equiv.py sequence nuitka $B/nuitka-arm64/arize-harness.bin /tmp/nuitka.json
$PY -I benchmark/checks/equiv.py diff /tmp/venv.json /tmp/nuitka.json   # 12 "same" lines
$PY -I benchmark/checks/equiv.py https $B/nuitka-universal/arize-harness.bin                    # 403 (fake key)
$PY -I benchmark/checks/equiv.py sessionkey $B/nuitka-universal/arize-harness.bin               # one state file, keyed by the driver's pid
arch -x86_64 .workbench/bt-x86_64/bin/python -I benchmark/checks/equiv.py https $B/nuitka-universal/arize-harness.bin
```
