# Binary spike: correctness checks (Arize-ai/arize#90274)

Builds under test, both made by the spike's `benchmark/build-nuitka.sh` / `build-pyi.sh` (on
`spike/benchmark-installs`) from the generated `build_inputs.py` lists, against venv312 (`pip install`
of the same tree, Python 3.12.13 from uv). Core/tracing code is identical to `05ee0c1`.

| build | executable |
|---|---|
| nuitka | `benchmark/builds/nuitka/arize-harness.dist/arize-harness.bin` (Nuitka 4.3, standalone) |
| pyi-onedir | `benchmark/builds/pyi-onedir/arize-harness/arize-harness` (PyInstaller 6.22.3, onedir) |
| venv312 | `benchmark/builds/venv312/bin/arize-harness` (baseline) |

Every run uses a throwaway HOME. No hook logic was changed.

## Results

| # | check | nuitka | pyi-onedir | workaround needed |
|---|---|---|---|---|
| 1 | same OTLP payloads + state as venv312 (Claude, Gemini, Codex; dry-run off, local sink) | **pass** | **pass** | none |
| 2 | real HTTPS to otlp.arize.com, fake key; SSL_CERT_FILE / SSL_CERT_DIR honoured | **pass** (403) | **pass** (403) | none |
| 3 | `arize-harness status` same as venv312 | **n/a**: no `status` subcommand at this commit | **n/a**: same | CLI change (probe entry) + `--include-module core.setup.status` → then identical |
| 4 | opencode/omp setup find `plugin/arize-tracing.ts`; devin setup finds `skills/` | **n/a**: setup not reachable, `tracing.*.install` not bundled | **n/a**: same | CLI change (probe entry) + 3 hand-written includes → then identical |
| 5 | no duplicate core packages via the `tracing/{claude_code,cursor}/core` symlinks | **pass** | **pass** | none |
| 6 | session key with no session_id matches venv312 | **pass** | **pass** | none |
| 7 | ad hoc sign, `--options runtime`, no entitlements: verify --strict + hooks run | **fail**: signs/verifies, then dyld refuses libpython | **fail**: same | `com.apple.security.cs.disable-library-validation`, or a real Developer ID signature |
| 8 | universal2 build: `lipo -archs`, runs under `arch -x86_64` | **fail**: Nuitka refuses `--macos-target-arch=universal` | **fail**: builds, but libpython is arm64 only → dlopen fails under x86_64 | a universal2 CPython (python.org) to build from |
| 9 | oldest supported macOS (minos) | **11.0** (both Mach-O files) | **11.0** (both Mach-O files) | none |

### Verdicts

- **nuitka:** passes cleanly on 1, 2, 5, 6 and 9. 3 and 4 can't run because the product CLI doesn't expose them, and that applies equally to both tools. Fails 7 (hardened runtime with ad hoc signing; the cause isn't Nuitka-specific) and 8 (needs a universal2 interpreter). No Nuitka-specific failure.
- **pyi-onedir:** the same results check for check: passes cleanly on 1, 2, 5, 6 and 9, n/a on 3 and 4, fails 7 and 8 for the same reasons. Unlike Nuitka, its universal2 failure is silent: the build succeeds and only crashes when run under x86_64.

Correctness doesn't separate the two builds: every difference found is shared and comes from the
product or the platform. So the decision goes back to speed (Nuitka, about 10 ms per hook) and to the
check 8 trap, which also favours Nuitka. Nuitka fails loudly, while PyInstaller ships a broken "universal2".

## Details and exact commands

Paths below: `M=/Users/cgong/Desktop/coding-harness-tracing` (the spike checkout with the builds and
`build_inputs.py`), `B=$M/benchmark/builds`. Run the scripts from `benchmark/checks/` with the system
`python3 -I`.

### 1. Same output as venv312

`checks/equiv.py sequence` runs three fixed sequences through `<cmd> hook ...` in a fresh HOME with
`ARIZE_DRY_RUN=false`. An Arize backend points at a local HTTP sink (`http://127.0.0.1:<port>`, so the
body is OTLP JSON). The script stores every request (path, Content-Type/authorization/space_id headers,
body), every state file and every logged error. Ids are numbered by first appearance, epoch-ms/ns and
time-keyed values become `T`, and the HOME path becomes `$HOME`. `equiv.py diff` must find everything else equal.

- Claude: SessionStart, UserPromptSubmit, Pre/Post ×3 (Read), Stop with a v2 transcript (workload.py's
  `turn_lines`). 1 request with 8 spans (Turn, 4 LLM, 3 Read); 1 state file.
- Gemini: session-start, before-agent, before/after-model, before/after-tool, after-agent, session-end.
  The sink holds each response for 1 s. Hooks exit in ≤0.06 s on every build, and all 3 spans (LLM, Turn, read_file) arrive
  afterwards from the double-forked grandchild. after-model's span lands after its hook has exited.
- Codex: `hook codex notify '<json>'` (payload in argv[1]) with a rollout file under `CODEX_HOME`.
  1 request (Turn + exec_command).

```bash
python3 -I equiv.py sequence venv312 $B/venv312/bin/arize-harness /tmp/venv312.json
python3 -I equiv.py sequence nuitka $B/nuitka/arize-harness.dist/arize-harness.bin /tmp/nuitka.json
python3 -I equiv.py sequence pyi-onedir $B/pyi-onedir/arize-harness/arize-harness /tmp/pyi-onedir.json
python3 -I equiv.py diff /tmp/venv312.json /tmp/nuitka.json       # 12/12 "same"
python3 -I equiv.py diff /tmp/venv312.json /tmp/pyi-onedir.json   # 12/12 "same"
```

Sanity: venv312 against a second venv312 run is also 12/12 "same", so the normalization is stable. A limitation:
attributes whose key looks time-like (for example `codex.turn.duration_ms`) are normalized even when they
come from fixed rollout timestamps, so a time-parsing difference there would not show up.

### 2. Real HTTPS

`equiv.py https <cmd>` sends one Claude turn to `otlp.arize.com:443` with key `equiv-fake-key`. All
three builds log `Arize send failed: HTTP 403: {"code":7,"message":"invalid Space ID"}`, with no SSL error.
Both overrides behave on the builds exactly as they do on venv312:

| env | venv312 | nuitka | pyi-onedir |
|---|---|---|---|
| (none) | 403 | 403 | 403 |
| `SSL_CERT_FILE=bogus-ca.pem SSL_CERT_DIR=<empty dir>` | CERTIFICATE_VERIFY_FAILED | same | same |
| `SSL_CERT_FILE=/etc/ssl/cert.pem SSL_CERT_DIR=<empty dir>` | 403 | 403 | 403 |
| `SSL_CERT_FILE=bogus-ca.pem SSL_CERT_DIR=<hashed copy of /etc/ssl/cert.pem>` | 403 | 403 | 403 |

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout k.pem -out bogus-ca.pem -subj /CN=bogus-ca -days 2
awk '/BEGIN CERT/{n++; f="split/c" n ".pem"} n{print > f}' /etc/ssl/cert.pem   # then link each as hashed/<x509 -hash>.N
SSL_CERT_FILE=$PWD/bogus-ca.pem SSL_CERT_DIR=$PWD/empty python3 -I equiv.py https <cmd>
```

The uv (python-build-standalone) interpreter links OpenSSL statically into `libpython3.12.dylib`. Both
builds bundle that same dylib, so they keep the same default CA paths and env handling.

### 3. `arize-harness status`

At this commit `core/cli.py` dispatches only `hook`. Status is `python -m core.setup.status`
(install.sh:394), so `arize-harness status` prints the usage line on all three builds. They match, but the
check tells us nothing. Clean builds also don't contain `core.setup.status`.

To see whether a frozen build *would* match, `checks/build-probe.sh` builds both tools again with the
same flags and generated lists, plus `checks/probe_entry.py`. The probe entry adds `status` and
`setup <harness>` on top of `core.cli`, and the build hand-includes `core.setup.status` and
`tracing.{opencode,omp,devin}.install`. `checks/check34.sh` then compares `status`, `status --json`,
the three setups and `status` after setup in one fixed fake HOME. Its output is **identical** to venv312
for both probe builds.

```bash
REPO=$M OUT=/tmp/probe ./build-probe.sh < /dev/null
./check34.sh venv $B/venv312 $M > venv.txt
./check34.sh probe /tmp/probe/nuitka/arize-harness.bin > nuitka.txt;   diff venv.txt nuitka.txt
./check34.sh probe /tmp/probe/pyi-onedir/arize-harness > pyi.txt;      diff venv.txt pyi.txt
```

### 4. Data files found through `__file__`

The baseline is venv312's python running the repo's `tracing/<h>/install.py` as a file, which is how
install.sh does it (install.sh:133). venv312's site-packages has no `plugin/*.ts` at all. Each setup is a
real (non-dry-run) `install --with-skills` in the fake HOME, because in dry-run the installers never
resolve the `.ts` source. With the probe builds:

- opencode / omp: `_plugin_source()` and `constants.PLUGIN_SOURCE` resolve inside the bundle
  (`<dist>/tracing/<h>/plugin/arize-tracing.ts` for Nuitka, `<dist>/_internal/tracing/...` for PyInstaller),
  the files exist, and the copied plugin files match venv312's byte for byte (check34.sh hashes).
- devin: `tracing/devin/skills/` does not exist in the repo ("This harness ships no skills yet",
  devin/install.py:98). `build_inputs.py data` already reports it as missing, and `_skills_dir_exists()`
  is False on all three builds, so nothing is lost. Note: `symlink_skills` reads
  `INSTALL_DIR/tracing/<h>/skills`, not the bundle, so a binary-only install would never link skills.
- devin/install.py has no `main()`; its CLI lives inline under `if __name__ == "__main__"`. A binary
  dispatcher needs it refactored into a function, which probe_entry.py works around.

```bash
/tmp/probe/nuitka/arize-harness.bin probe-data
/tmp/probe/pyi-onedir/arize-harness probe-data
```

### 5. Duplicate core packages through the symlinks

`tracing/claude_code/core` and `tracing/cursor/core` are symlinks to `../../core`. Both module lists hold
the same 65 `core.*` / `tracing.*` modules, and neither has any `tracing.claude_code.core.*` or
`tracing.cursor.core.*`.

```bash
grep -o '<module name="[^"]*"' $M/.workbench/nuitka/compilation-report.xml | sed 's/.*="//;s/"//' | grep -E '^(core|tracing)'
python3 -c "import ast;t=ast.literal_eval(open('$M/.workbench/pyi-onedir/build/arize-harness/PYZ-00.toc').read());print(sorted(e[0] for e in t[1] if e[0].split('.')[0] in ('core','tracing')))"
```

### 6. Session key

`equiv.py sessionkey <cmd>` runs `/bin/sh -c` twice, piping a SessionStart payload with no `session_id`
into `<cmd> hook claude session-start` each time. On every build the two hooks share one state file,
`state_<driver pid>.json`: the grandparent pid, as on venv312. PyInstaller onedir adds no extra process,
so the key stays the same.

### 7. Ad hoc signing with hardened runtime

On writable copies of each build, sign the inner dylib first and then the executable:

```bash
find <dist> -name '*.dylib' -exec codesign --force --sign - --options runtime --timestamp=none {} \;
codesign --force --sign - --options runtime --timestamp=none <exe>
codesign --verify --strict <exe>          # "valid on disk ... satisfies its Designated Requirement" (both)
<exe> hook --list                         # nuitka: SIGABRT (134), pyi-onedir: exit 255
```

dyld rejects the dylib: `code signature in ... libpython3.12.dylib not valid for use in process: mapping
process and mapped file (non-platform) have different Team IDs`. With hardened runtime, library validation
needs a matching Team ID, and ad hoc signatures have none. The same signing *without* `--options runtime`
runs (38 hooks listed). Adding the `com.apple.security.cs.disable-library-validation` entitlement to the
executable also runs. That entitlement is outside the clean criteria; the real fix is a Developer ID signature.

Side finding: `build_inputs.py check` reported "38/38 loaded" for both crashing binaries. It only greps
stderr for "could not load hook" and never checks the exit status.

### 8. Universal binary

The only interpreters on this machine are arm64-only uv builds (`lipo -archs` → `arm64`).

- Nuitka: `--macos-target-arch=universal` → `error: option --macos-target-arch: invalid choice:
  'universal' (choose from 'arm64', 'x86_64')`. No build.
- PyInstaller: `--target-arch universal2` builds with no warning. `lipo -archs`: executable `x86_64 arm64`,
  `_internal/libpython3.12.dylib` `arm64`. It runs natively, but under `arch -x86_64` it fails with
  `Failed to load Python shared library ... incompatible architecture (have 'arm64', need 'x86_64')`.
- The current builds are `arm64` only and `arch -x86_64` can't spawn them.

Both need a universal2 CPython (the python.org installer), or two single-arch builds merged with `lipo`.

### 9. Oldest supported macOS

Each bundle has exactly two Mach-O files, because python-build-standalone links its extension modules
into libpython and leaves no `lib-dynload/*.so`. `otool -l` (LC_BUILD_VERSION minos):

| build | file | minos |
|---|---|---|
| nuitka | `arize-harness.bin` | 11.0 |
| nuitka | `libpython3.12.dylib` | 11.0 |
| pyi-onedir | `arize-harness` | 11.0 |
| pyi-onedir | `_internal/libpython3.12.dylib` | 11.0 |

## Other findings

- Under `--quiet`, `build-nuitka.sh` stops at Nuitka's interactive "download ccache?" prompt when stdin
  is not a TTY but stays open, as in CI or an agent shell. With `< /dev/null` it defaults to "no" and the
  build goes on.
- The installers write `~/.arize/harness/venv/bin/arize-hook-*` (devin) and a venv-based launcher (the
  opencode/omp plugins) into harness configs. A binary distribution needs setup to write the binary's path.
