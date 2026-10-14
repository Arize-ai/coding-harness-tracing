# Per-turn hook cost (Claude Code, venv312)

This follows up the binary feasibility spike (Arize-ai/arize#90274). `benchmark/run-all.sh` compares
packaging formats on startup: SessionStart and a near-empty Stop, run back to back. This benchmark
measures what the hooks cost during a real turn on one fixed install, the venv312 console scripts.
Every Claude hook is synchronous: PreToolUse delays the tool, PostToolUse delays the next model call,
and Stop and UserPromptSubmit delay the turn boundaries. Only measurement code was added. The hook
logic is unchanged.

```bash
benchmark/workload/run-all.sh            # ~15 min; SKIP_REMOTE=1 skips the otlp.arize.com requests
REPLAY=~/.claude/projects/<proj>/<session>.jsonl benchmark/workload/run-all.sh   # adds the replay
benchmark/workload/workload.py <scenario> --quick   # quick check that the harness works
```

**Setup.** Every scenario runs with its own throwaway HOME. Dry-run is off, and Stop sends to a
threaded local HTTP sink unless the scenario sets another endpoint. The Arize JSON target is used.
Each scenario has a gate: every timed Stop must reach the sink, and every observation must be
acknowledged. All gates passed. Times are wall clock, from the driver's spawn to the hook's exit,
measured on an Apple M5 Pro running macOS 26.7.1.

**Machine caveat.** An endpoint-security client (`endpointsecurityd`) is active on this machine.
Every `exec` costs about 7 ms here (`/usr/bin/true` takes 7.0 ms) and every shell hop about 10 ms.
So any cost caused by launching extra processes is larger here than on an unmanaged Mac. Managed
(MDM) laptops are the main audience, so these figures are probably representative.

Raw results, ignored by git, are in `benchmark/results/workload/<scenario>.{json,md}`.

## Results

### 1. Concurrency: N parallel PreToolUse, then N parallel PostToolUse, on one session

Each round starts all N Pre hooks at once, waits for them, then starts all N Post hooks at once. Each
Post carries a 1 KB `tool_response`. A probe on `PYTHONPATH`
(`instrument/sitecustomize.py`) counts the 100 ms sleeps and forced breaks in `FileLock._acquire_fcntl`.

| hooks | n | p50 | p95 | p99 | max (ms) |
|---|---|---|---|---|---|
| N=1 PreToolUse | 80 | 45.8 | 52.5 | 55.3 | 55.3 |
| N=1 PostToolUse | 80 | 46.0 | 53.3 | 64.3 | 64.3 |
| N=4 PreToolUse | 80 | 56.7 | 268 | 365 | 365 |
| N=4 PostToolUse | 80 | 162 | 281 | 387 | 387 |
| N=8 PreToolUse | 80 | 68.2 | 273 | 387 | 387 |
| N=8 PostToolUse | 80 | 76.9 | 287 | 397 | 397 |

| N | processes that slept on the lock | 100 ms sleeps (total / max in one process) | forced breaks (3 s) | lost observations | slowest batch, Pre / Post (ms) |
|---|---|---|---|---|---|
| 1 | 0/160 | 0 / 0 | 0 | 0/80 | 56 / 65 |
| 4 | 81/160 | 116 / 3 | 0 | 0/80 | 366 / 388 |
| 8 | 62/160 | 80 / 3 | 0 | 0/80 | 387 / 399 |

### 2. Buffer growth: `claude.tool_observation_buffer` pre-filled, then one Pre, Post and Stop

The pre-filled tools belong to the open turn and appear in its transcript, so Stop matches and
exports all of them. The transcript's own tool results are kept small to isolate the buffer cost.

| buffered tools | tool_response | state file (MB) | Pre p50 | Post p50 | Stop p50 | Stop max (ms) |
|---|---|---|---|---|---|---|
| 0 | 1 KB | 0.00 | 44.9 | 45.4 | 53.8 | 58.7 |
| 10 | 1 KB | 0.02 | 44.5 | 45.7 | 53.4 | 59.4 |
| 50 | 1 KB | 0.07 | 45.9 | 46.4 | 56.5 | 60.7 |
| 200 | 1 KB | 0.27 | 49.0 | 51.4 | 66.9 | 72.2 |
| 0 | 50 KB | 0.05 | 46.3 | 46.3 | 55.2 | 59.8 |
| 10 | 50 KB | 0.57 | 48.7 | 51.9 | 62.7 | 66.1 |
| 50 | 50 KB | 2.63 | 58.5 | 67.3 | 90.3 | 94.7 |
| 200 | 50 KB | 10.4 | 104 | 141 | 219 | 235 |
| 0 | 500 KB | 0.51 | 45.9 | 49.8 | 60.6 | 64.5 |
| 10 | 500 KB | 5.64 | 74.7 | 92.4 | 140 | 155 |
| 50 | 500 KB | 26.1 | 184 | 268 | 437 | 454 |
| 200 | 500 KB | 103 | 591 | 895 | 1,678 | 1,742 |

### 3. Transcript size: UserPromptSubmit, then Stop, with a fixed 2-tool last turn

| transcript (MB) | UserPromptSubmit p50 | UPS max | Stop p50 | Stop max (ms) |
|---|---|---|---|---|
| 0.01 | 46.3 | 83.9 | 53.7 | 87.0 |
| 1.06 | 44.6 | 52.2 | 54.1 | 67.4 |
| 10.5 | 47.6 | 48.4 | 62.5 | 64.7 |
| 52.4 | 62.4 | 66.8 | 107 | 110 |

### 4. Invocation path (60 runs each, back to back)

- `run-hook`: `scripts/run-hook` with CLAUDE_PLUGIN_ROOT and CLAUDE_PLUGIN_DATA set and the venv
  already bootstrapped. `python3` on PATH is venv312's interpreter.
- `launcher`: the bootstrapped `$CLAUDE_PLUGIN_DATA/venv/bin/arize-hook-*` called directly. It is
  `/bin/sh`, then `exec python -c`.
- `console`: venv312's pip `arize-hook-*` script.
- `harness`: `arize-harness hook claude <event>`.

| event | run-hook | launcher | console | harness (p50 ms) |
|---|---|---|---|---|
| SessionStart | 130 | 68.9 | 44.5 | 43.0 |
| PreToolUse | 133 | 66.0 | 42.6 | 42.3 |
| Stop | 141 | 73.2 | 50.4 | 52.1 |

Two experiments explain the gaps. Wrapping the console script in `/bin/sh -c 'exec …'` costs
+22 ms, and `env FOO=1 …` costs +10 ms. So the launcher's ~23 ms is its shell hop. run-hook adds
~65 ms on top of that: a `/bin/sh`, then two `python3 -c` calls (the `find_python` version check and
the `needs_install` hash), and then it execs the launcher.

### 4b. Idle gap: back-to-back runs vs runs after a pause (20 runs each)

| hook | after 0 s | after 0.5 s | after 2 s | after 5 s idle (p50 ms) |
|---|---|---|---|---|
| PreToolUse | 46.4 | 69.9 | 70.0 | 68.9 |
| Stop | 52.4 | 77.5 | 78.9 | 78.0 |

### 5. Network, dry-run off: three turns of 3 tools (10 KB responses each)

| endpoint | turn | UserPromptSubmit (ms) | Stop (ms) | failed sends | buffered tools after Stop | state (KB) | trace_count |
|---|---|---|---|---|---|---|---|
| local sink | 1 / 2 / 3 | 49 / 44 / 46 | 53 / 54 / 49 | 0 / 0 / 0 | 0 / 0 / 0 | 0.2 | 1 / 2 / 3 |
| otlp.arize.com, fake key | 1 / 2 / 3 | 47 / 260 / 309 | 252 / 298 / 310 | 1 / 2 / 2 | 3 / 6 / 9 | 32 / 64 / 96 | 1 / 1 / 1 |
| refused port | 1 / 2 / 3 | 44 / 52 / 78 | 54 / 52 / 82 | 1 / 2 / 2 | 3 / 6 / 9 | 32 / 64 / 96 | 1 / 1 / 1 |
| blackholed IP (10.255.255.1) | 1 / 2 / 3 | 80 / 10,126 / 10,081 | 10,101 / 10,085 / 10,067 | 1 / 2 / 2 | 3 / 6 / 9 | 32 / 64 / 96 | 1 / 1 / 1 |

The fake key got `HTTP 403 invalid Space ID`, not the expected 401: the fake space id is checked
first. Either way it is a rejected send.

### 6. End-to-end replay of a recorded turn

I replayed the last turn of a recorded local session: 19 tools, 40 hook events, 181 s recorded, and
89 s once each gap was capped at 5 s. Only timestamps and tool ids were read from the transcript.
Each hook blocks the schedule, as in Claude Code. The tools ran serially.

| | wall (s) | hook time (ms) | per hook p50 / p95 / max (ms) |
|---|---|---|---|
| without hooks | 88.87 | 0 | — |
| with hooks | 91.71 | 2,846 | 69 / 84 / 121 |

The hooks added 2.85 s to the turn, or 3.2%. That is ~71 ms per hook, not the ~45 ms measured
back to back; see 4b.

## What grows, and how fast

The fit is a line through the p50s in each table.

| cost | grows with | rate | where it starts to hurt |
|---|---|---|---|
| Lock contention | parallel tool calls per session | flat at N=1. At N≥4, about half the processes sleep 1–3 × 100 ms; p95 is ~270–290 ms and max ~390 ms | any parallel tool batch: Claude waits ~0.4 s per batch, Pre and Post each |
| State rewrite (Pre/Post) | bytes in the tool buffer = open-turn tools × response size | Pre ~5 ms/MB, Post ~8 ms/MB | ~10 MB, e.g. 20 large reads in one turn |
| Stop export | same buffer bytes | ~15 ms/MB (1.7 s at 103 MB) | the same ~10 MB |
| Transcript re-read | session transcript size | Stop ~1.0 ms/MB, UPS ~0.3 ms/MB | only past ~50 MB (+55 ms) |
| Failed export | turns since the backend failed | every turn pays 2 sends (UPS retry + Stop). The turn never closes, so tools pile into one buffer (+3 tools, +32 KB per turn here) | at once with a blackhole: ~20 s per turn; with a 403, +0.5 s per turn |
| Extra launches (run-hook) | constant per hook | +87 ms over the console script (~65 ms from run-hook itself, ~23 ms from the launcher's shell) | every hook, ~40 per 20-tool turn: ~3.5 s per turn |
| Idle wake-up | constant per hook | +25 ms after ≥0.5 s idle | every hook in a real session |

What this means:

1. **The lock's poll interval is the largest per-tool cost.** A state update holds the lock for a few
   ms, but a waiter checks again only every 100 ms. One collision therefore costs 100 ms, and three
   collisions cost 300 ms. Nothing was lost and the lock was never force-broken, so the lock is
   correct, just slow. Shorter polling or a blocking `flock` with a timeout would fix it without
   any change to packaging.
2. **Every byte in the buffer is paid for again on each Pre and Post.** Each Pre/Post rewrites the
   whole state file several times (`set`, `record_start`, `increment`, `record_result`, `delete`).
   The buffer is stored as a JSON string inside JSON, so it is decoded twice on each read. Stop then
   encodes everything once more for the payload. Cost scales with tool-response bytes in the open
   turn, not with tool count: 200 tools of 1 KB cost almost nothing.
3. **One failure keeps costing on every turn.** After a failed Stop, `current_trace_id` stays set.
   Each later UserPromptSubmit retries the old turn inline (handlers.py:370), fails, and returns
   before it opens a new turn (`trace_count` stays at 1). So each failing turn sends twice, and with
   a blackholed endpoint each send blocks for the full 10 s urllib timeout (core/common.py:650): the
   user waits ~10 s on submit and again on Stop. Every later turn's tools join the same buffer, so
   cost 2 grows with every turn the backend stays down. A refused port fails fast, so it costs
   little.
4. **Transcript size matters less than expected.** Re-reading 50 MB costs ~55 ms on Stop and
   ~16 ms on UPS. That is real but small next to items 1–3.
5. **The run-hook path costs as much as the hook itself.** A plugin-marketplace install pays
   ~130 ms per hook where the console script pays ~43 ms. Packaging can remove part of this: run-hook
   could exec the venv python without the two version/hash `python3 -c` calls and without the shell
   launcher. Benchmarks run back to back also understate every hook by ~25 ms (4b), so budgets set
   from the spike's warm numbers are too optimistic.

## Per-hook cost for every build

`HOOKS_ONLY=1 BUILDS=<builds> benchmark/run-all.sh` runs `workload.py hooks` on each build in
run-all.sh's CANDIDATES, as `<exe> hook claude <event>`. Setup matches bench.sh: throwaway HOME,
`ARIZE_DRY_RUN=true` plus `ARIZE_VERBOSE=true`, and the same gate (every timed Stop logs a dry-run
span; all six builds passed, 65/65). Each call follows a session reset and untimed setup calls, so it
does real work. There are 30 runs per hook per mode, with 5 warmups back to back. Raw results:
`benchmark/results/<label>/hooks.json`.

| build | SessionStart mean / p95 (ms) | Stop mean / p95 (ms) | first run, worst (ms) | first run read-only, worst (ms) | size | files | .so/.dylib | build time (s) | budget | UPS b2b p50 / p95 (ms) | Pre b2b p50 / p95 (ms) | Post b2b p50 / p95 (ms) | Stop b2b p50 / p95 (ms) | UPS idle 2s p50 / p95 (ms) | Pre idle 2s p50 / p95 (ms) | Post idle 2s p50 / p95 (ms) | Stop idle 2s p50 / p95 (ms) | 20-tool turn, idle (est.) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nuitka | 37.6 / 39.3 | 40.3 / 42.5 | 692 | 555 |  27M | 4 | 1 | 36 | PASS | 42 / 43 | 40 / 48 | 42 / 45 | 45 / 60 | 65 / 72 | 65 / 70 | 65 / 68 | 64 / 68 | 2.72 s |
| pbs | 63.2 / 67.5 | 61.6 / 67.1 | 576 | 539 |  87M | 3004 | 10 | 3 | PASS | 65 / 67 | 65 / 68 | 66 / 69 | 65 / 68 | 93 / 98 | 94 / 97 | 93 / 97 | 93 / 98 | 3.93 s |
| pbs-nocompile | 171.9 / 174.5 | 176.2 / 184.6 | 1209 | 561 |  67M | 1812 | 10 | 3 | FAIL | 181 / 193 | 178 / 193 | 183 / 198 | 179 / 183 | 214 / 217 | 212 / 230 | 213 / 230 | 213 / 222 | 8.94 s |
| pyi-onedir | 45.8 / 48.3 | 46.9 / 48.3 | 482 | 453 |  20M | 5 | 1 | 5 | PASS | 49 / 51 | 48 / 50 | 49 / 50 | 50 / 53 | 76 / 78 | 75 / 79 | 76 / 83 | 77 / 82 | 3.17 s |
| pyi-onefile | 346.5 / 383.1 | 351.9 / 427.3 | 630 | 569 | 9.3M | 1 | 0 | 15 | FAIL | 352 / 428 | 351 / 369 | 350 / 367 | 346 / 363 | 389 / 441 | 386 / 400 | 393 / 421 | 389 / 407 | 16.34 s |
| venv312 | 41.6 / 42.6 | 43.2 / 44.2 | 71 | 60 | 2.8M | 299 | 0 | - | baseline | 52 / 57 | 49 / 57 | 50 / 58 | 49 / 59 | 73 / 79 | 71 / 75 | 70 / 73 | 69 / 73 | 2.95 s |

Budget: warm p95 at most 25 ms slower than venv312 on both events. First-run times are reported, not judged against it.
Per-hook columns: 20-tool turn = UPS + 20×Pre + 20×Post + Stop, idle p50s; reported, not judged against the budget.

The ranking is the same as on startup: nuitka < venv312 < pyi-onedir < pbs < pbs-nocompile <
pyi-onefile. The gaps change, though:
- Idle adds 20–25 ms per hook on venv312 and nuitka, 27 ms on pyi-onedir, 28 ms on pbs, 33 ms on
  pbs-nocompile, and ~40 ms on pyi-onefile. So builds with larger trees lose more after a pause.
- Back to back, pyi-onedir ties venv312 (48–50 vs 49–52 ms). After idle it is ~5 ms slower per hook:
  +0.22 s per 20-tool turn.
- pbs passes the startup budget at the edge (+24.9 ms p95) but costs +1.0 s per 20-tool turn
  (3.93 vs 2.95 s).
- nuitka is the only build cheaper than venv312 per turn: −0.23 s, about 8%.

## Caveats

- The tool-response bytes from scenario 2 are not also put into the transcript. In a real turn they
  would be there too, so the costs in items 2 and 4 add up.
- Concurrency starts N processes from Python threads behind a barrier. Spawning eight processes
  staggers them by a few ms, which may explain why N=8 had fewer sleepers than N=4.
- Replay runs parallel tool calls one after another. Combined with scenario 1, a real parallel
  batch would cost more than the replay shows.
- All numbers come from one machine with endpoint security active, so process launch costs are
  higher than on an unmanaged Mac.
