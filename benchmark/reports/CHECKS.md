| | nuitka-arm64-3.12.10 | nuitka-universal |
|---|---|---|
| size / files | 28M / 4 | 57M / 4 |
| build time | 36 s | 85 s (36 + 49 + 0.4) |
| first run, writable: per copy (ms) | 553, 521, 503, 507, 493 | 902, **1805**, 865, 864, 869 |
| first run, read-only: per copy (ms) | 507, 511, 500, 503, 564 | 884, 872, 824, 876, 879 |
| SessionStart / Stop mean / p95 (ms) | 44.3 / 52.8, 43.7 / 52.4 | 41.1 / 42.8, 41.3 / 42.5 |
| UPS / Pre / Post / Stop p50 / p95, back to back (ms) | 43/47, 42/42, 42/43, 43/43 | 43/45, 43/44, 43/45, 43/45 |
| UPS / Pre / Post / Stop p50 / p95, after 2 s idle (ms) | 73/80, 72/77, 72/78, 74/78 | 74/81, 73/78, 73/80, 75/80 |
| 20-tool turn, idle (est.) | 3.04 s | 3.07 s |

**Does going universal matter?** Only on the first launch after install or update, which costs about
+360 ms (roughly 870 ms instead of 505 ms, once). Every later hook is within 1–2 ms of the arm64-only
build, so per-turn cost doesn't change. Size doubles to 57M and build time more than doubles, which
affects packaging but not users. (The arm64 build's higher startup p95 is noise: its per-hook p95s are
the lowest in the table.)
