#!/usr/bin/env python3
"""What a frozen build must include, generated from the code so a new hook can't be left out.

  inputs.py modules         every module imported by name at runtime, one per line:
                            each hook target in core/hook_table.py iter_hooks(), plus the
                            core.setup.status registration modules (status.py:62)
  inputs.py data            data files found via __file__, as "<abs source>\t<dest in bundle>";
                            a listed path that doesn't exist is reported on stderr, and any such
                            path not in EXPECTED_MISSING makes it exit 1
  inputs.py hooks           the exact `arize-harness hook --list` output a complete build must print
  inputs.py check EXE       smoke-test a build: `hook --list` must match `hooks` exactly, and every
                            hook must load (--list only prints the table, which is plain data, so a
                            build missing every handler still passes it) and exit 0: a crash, a
                            signal or a timeout is a failure too. Exit 1 on any failure.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from core.hook_table import iter_hooks  # noqa: E402

# Located via Path(__file__).parent in tracing/{opencode,omp}/constants.py, */install.py
# and tracing/devin/install.py:113. Dest mirrors the package path so __file__ math still works.
DATA = ["tracing/opencode/plugin/arize-tracing.ts", "tracing/omp/plugin/arize-tracing.ts", "tracing/devin/skills"]
# Listed in DATA but absent on purpose: the repo ships no devin skills yet ("This harness ships no
# skills yet", tracing/devin/install.py). Any other missing path fails `inputs.py data`.
EXPECTED_MISSING = {"tracing/devin/skills"}


def modules():
    from core.setup.status import _REGISTRATION

    names = {target.split(":", 1)[0] for _h, _e, target in iter_hooks()}
    names |= {module for module, _attrs in _REGISTRATION.values()}
    return sorted(names)


def data():
    missing = 0
    for rel in DATA:
        src = REPO / rel
        if not src.exists():
            print(f"build_inputs: data path missing, not included: {rel}", file=sys.stderr)
            missing += rel not in EXPECTED_MISSING
            continue
        files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
        for f in files:
            print(f"{f}\t{f.relative_to(REPO)}")
    return missing


def hooks():
    return [h if e is None else f"{h} {e}" for h, e, _t in iter_hooks()]


def check(exe):
    home = tempfile.mkdtemp()
    # Tracing off: handlers return right after loading. Dry-run in case one doesn't check.
    env = dict(os.environ, HOME=home, ARIZE_TRACE_ENABLED="false", ARIZE_DRY_RUN="true")
    exe = os.path.abspath(exe)  # the hooks run from the throwaway HOME

    def run(*a):
        """(CompletedProcess or None, problem). problem is None only for exit 0: a crashing build
        prints nothing about loading, so its exit status is the only sign that it failed."""
        try:
            p = subprocess.run(
                [exe, "hook", *a],
                cwd=home,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            return None, f"timed out after {e.timeout:g} s"
        if p.returncode == 0:
            return p, None
        how = f"exit {p.returncode}" if p.returncode > 0 else f"killed by signal {-p.returncode}"
        last = p.stderr.strip().splitlines()[-1:]
        return p, how + (f": {last[0]}" if last else "")

    failures = bad_hooks = 0
    p, problem = run("--list")
    if problem:
        failures += 1
        print(f"check: hook --list: {problem}", file=sys.stderr)
    listed = [] if problem else p.stdout.splitlines()
    if listed != hooks():
        failures += 1
        print("check: hook --list does not match iter_hooks()", file=sys.stderr)
        for line in sorted(set(hooks()) ^ set(listed)):
            print(f"  {'missing' if line in hooks() else 'extra'}: {line}", file=sys.stderr)
    for hook in hooks():
        p, problem = run(*hook.split())
        if not problem and "could not load hook" in p.stderr:
            problem = p.stderr.strip().splitlines()[-1]
        if problem:
            failures += 1
            bad_hooks += 1
            print(f"check: {hook}: {problem}", file=sys.stderr)
    shutil.rmtree(home, ignore_errors=True)
    print(
        f"check: {len(listed)} hooks listed, {len(hooks()) - bad_hooks}/{len(hooks())} loaded and exited 0, "
        f"{failures} failure(s)"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "modules":
        print("\n".join(modules()))
    elif what == "data":
        sys.exit(1 if data() else 0)
    elif what == "hooks":
        print("\n".join(hooks()))
    elif what == "check" and len(sys.argv) == 3:
        sys.exit(check(sys.argv[2]))
    else:
        sys.exit(__doc__)
