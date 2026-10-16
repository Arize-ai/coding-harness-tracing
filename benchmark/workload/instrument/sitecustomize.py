"""Workload benchmark probe: counts FileLock waits in a hook process. Measurement only.

workload.py puts this directory on PYTHONPATH (both the venv312 console scripts and
run-hook's launchers honor it) and sets WORKLOAD_LOCK_LOG. Python then imports this
module at startup. It wraps time.sleep and Path.unlink so that calls made directly from
core/common.py's FileLock._acquire_* are counted: each sleep is one 100 ms poll, each
unlink is a forced break after the 3 s timeout. The hook's own logic is untouched.
At exit it appends one JSON line per process to WORKLOAD_LOCK_LOG.
"""
import os

_LOG = os.environ.get("WORKLOAD_LOCK_LOG")

if _LOG:
    import atexit
    import json
    import pathlib
    import sys
    import time

    _counts = {"sleeps": 0, "breaks": 0}
    _sleep, _unlink = time.sleep, pathlib.Path.unlink

    def _from_lock() -> bool:
        return sys._getframe(2).f_code.co_name.startswith("_acquire")

    def _counting_sleep(seconds):
        if _from_lock():
            _counts["sleeps"] += 1
        return _sleep(seconds)

    def _counting_unlink(self, *args, **kwargs):
        if _from_lock():
            _counts["breaks"] += 1
        return _unlink(self, *args, **kwargs)

    time.sleep = _counting_sleep
    pathlib.Path.unlink = _counting_unlink

    @atexit.register
    def _report():
        line = dict(_counts, pid=os.getpid(), tag=os.environ.get("WORKLOAD_TAG", ""))
        with open(_LOG, "a") as f:
            f.write(json.dumps(line) + "\n")
