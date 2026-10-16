#!/usr/bin/env python3
"""Per-phase process lifecycle timing for one hook invocation (Arize-ai/arize#90274).

hyperfine gives one wall-clock number. This splits it, per run, into:
  spawn     parent calls posix_spawn -> posix_spawn returns   (process creation)
  boot      spawn returned -> child's first Python line runs  (exec, code signing, dyld,
                                                               EDR exec checks, interpreter init)
  run       first line -> child exits, as seen by the parent  (imports, handler, teardown)
plus the child's CPU time from wait4() rusage. wall - (user + sys) is time the child spent
blocked rather than computing: disk, an Endpoint Security client deciding on an exec or
open, scheduling. Where that gap grows is where the security agent is.

  lifecycle.py [--runs N] [--event E] [--payload FILE] [--concurrency C] [--shell] -- CMD...

CMD is the hook command without "hook claude <event>". Python builds get a probe: pass the
interpreter (e.g. builds/pbs/bin/python3) and --python, and the first-line timestamp is
written to fd 3. Frozen builds get spawn/total/rusage only. monotonic_ns is system-wide on
macOS (mach_absolute_time), so parent and child timestamps compare directly.

Runs in a throwaway HOME with ARIZE_DRY_RUN=true, like bench.sh.
"""
import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

PROBE = (
    "import os,time;os.write(3,b'%d'%time.monotonic_ns());os.close(3);"
    "import runpy,sys;sys.argv=['arize-harness',*sys.argv[1:]];"
    "runpy.run_module('core.cli',run_name='__main__',alter_sys=True)"
)


def one_run(argv, payload, env, probe):
    stdin_r, stdin_w = os.pipe()
    stamp_r, stamp_w = os.pipe() if probe else (None, None)
    actions = [(os.POSIX_SPAWN_DUP2, stdin_r, 0)]
    devnull = os.open(os.devnull, os.O_WRONLY)
    actions += [(os.POSIX_SPAWN_DUP2, devnull, 1), (os.POSIX_SPAWN_DUP2, devnull, 2)]
    if probe:
        actions.append((os.POSIX_SPAWN_DUP2, stamp_w, 3))
    t0 = time.monotonic_ns()
    pid = os.posix_spawn(argv[0], argv, env, file_actions=actions)
    t_spawned = time.monotonic_ns()
    for fd in (stdin_r, devnull, stamp_w):
        if fd is not None:
            os.close(fd)
    os.write(stdin_w, payload)  # payloads over the 16-64 KB pipe buffer block here until the child reads
    os.close(stdin_w)
    _, status, ru = os.wait4(pid, 0)
    t_exit = time.monotonic_ns()
    t_main = None
    if probe:
        raw = os.read(stamp_r, 64)
        os.close(stamp_r)
        t_main = int(raw) if raw else None
    us = lambda a, b: (b - a) / 1e3
    return dict(
        status=os.waitstatus_to_exitcode(status),
        spawn=us(t0, t_spawned),
        boot=us(t_spawned, t_main) if t_main else None,
        run=us(t_main, t_exit) if t_main else None,
        total=us(t0, t_exit),
        cpu=(ru.ru_utime + ru.ru_stime) * 1e6,
        blocked=us(t0, t_exit) - (ru.ru_utime + ru.ru_stime) * 1e6,
        maxrss_mb=ru.ru_maxrss / 2**20,  # bytes on macOS, KB on Linux
        majflt=ru.ru_majflt,
    )


def summarize(rows):
    print(f"{'phase':<10}{'median':>10}{'p95':>10}{'p99':>10}{'max':>10}   (us; maxrss in MB)")
    for key in ("spawn", "boot", "run", "total", "cpu", "blocked", "maxrss_mb", "majflt"):
        v = sorted(r[key] for r in rows if r[key] is not None)
        if not v:
            continue
        q = lambda p: v[min(len(v) - 1, int(p * (len(v) - 1)))]
        print(f"{key:<10}{statistics.median(v):>10.0f}{q(.95):>10.0f}{q(.99):>10.0f}{v[-1]:>10.0f}")
    bad = sum(r["status"] != 0 for r in rows)
    if bad:
        print(f"WARNING: {bad} run(s) exited non-zero", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--event", default="session-start")
    ap.add_argument("--payload", help="stdin file (default: a minimal SessionStart)")
    ap.add_argument("--concurrency", type=int, default=1, help="hooks in flight at once, like parallel tool calls")
    ap.add_argument("--python", action="store_true", help="CMD is a Python interpreter: add the fd-3 probe")
    ap.add_argument("--shell", action="store_true", help="wrap in /bin/sh -c, as harnesses run hook commands")
    ap.add_argument("cmd", nargs="+")
    a = ap.parse_args()

    base = [os.path.abspath(shutil.which(a.cmd[0]) or a.cmd[0]), *a.cmd[1:]]
    base += ["-I", "-c", PROBE] if a.python else []
    argv = [*base, "hook", "claude", a.event]
    if a.shell:
        import shlex

        argv = ["/bin/sh", "-c", shlex.join(argv)]
    payload = open(a.payload, "rb").read() if a.payload else b'{"session_id":"s1","hook_event_name":"SessionStart"}'

    home = tempfile.mkdtemp()
    env = dict(os.environ, HOME=home, ARIZE_DRY_RUN="true")
    try:
        os.chdir(home)  # -I aside, keep the repo's core/ off any implicit sys.path
        run = lambda _: one_run(argv, payload, env, a.python)
        with ThreadPoolExecutor(a.concurrency) as pool:
            list(pool.map(run, range(a.warmup)))
            rows = list(pool.map(run, range(a.runs)))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print(f"{' '.join(argv)[:110]}  runs={a.runs} concurrency={a.concurrency}")
    summarize(rows)


if __name__ == "__main__":
    main()
