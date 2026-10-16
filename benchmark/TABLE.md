| build | SessionStart mean / p95 (ms) | Stop mean / p95 (ms) | first run, worst (ms) | first run read-only, worst (ms) | size | files | .so/.dylib | build time (s) | budget | UPS b2b p50 / p95 (ms) | Pre b2b p50 / p95 (ms) | Post b2b p50 / p95 (ms) | Stop b2b p50 / p95 (ms) | UPS idle 2s p50 / p95 (ms) | Pre idle 2s p50 / p95 (ms) | Post idle 2s p50 / p95 (ms) | Stop idle 2s p50 / p95 (ms) | 20-tool turn, idle (est.) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nuitka | 37.6 / 39.3 | 40.3 / 42.5 | 692 | 555 |  27M | 4 | 1 | 36 | PASS | 42 / 43 | 40 / 48 | 42 / 45 | 45 / 60 | 65 / 72 | 65 / 70 | 65 / 68 | 64 / 68 | 2.72 s |
| nuitka-arm64-3.12.10 | 44.3 / 52.8 | 43.7 / 52.4 | 553 | 564 |  28M | 4 | 1 | 36 | PASS | 43 / 47 | 42 / 42 | 42 / 43 | 43 / 43 | 73 / 80 | 72 / 77 | 72 / 78 | 74 / 78 | 3.04 s |
| nuitka-universal | 41.1 / 42.8 | 41.3 / 42.5 | 1805 | 884 |  57M | 4 | 1 | 85 | PASS | 43 / 45 | 43 / 44 | 43 / 45 | 43 / 45 | 74 / 81 | 73 / 78 | 73 / 80 | 75 / 80 | 3.07 s |
| pbs | 63.2 / 67.5 | 61.6 / 67.1 | 576 | 539 |  87M | 3004 | 10 | 3 | PASS | 65 / 67 | 65 / 68 | 66 / 69 | 65 / 68 | 93 / 98 | 94 / 97 | 93 / 97 | 93 / 98 | 3.93 s |
| pbs-nocompile | 171.9 / 174.5 | 176.2 / 184.6 | 1209 | 561 |  67M | 1812 | 10 | 3 | FAIL | 181 / 193 | 178 / 193 | 183 / 198 | 179 / 183 | 214 / 217 | 212 / 230 | 213 / 230 | 213 / 222 | 8.94 s |
| pyi-onedir | 45.8 / 48.3 | 46.9 / 48.3 | 482 | 453 |  20M | 5 | 1 | 5 | PASS | 49 / 51 | 48 / 50 | 49 / 50 | 50 / 53 | 76 / 78 | 75 / 79 | 76 / 83 | 77 / 82 | 3.17 s |
| pyi-onefile | 346.5 / 383.1 | 351.9 / 427.3 | 630 | 569 | 9.3M | 1 | 0 | 15 | FAIL | 352 / 428 | 351 / 369 | 350 / 367 | 346 / 363 | 389 / 441 | 386 / 400 | 393 / 421 | 389 / 407 | 16.34 s |
| venv312 | 41.6 / 42.6 | 43.2 / 44.2 | 71 | 60 | 2.8M | 299 | 0 | - | baseline | 52 / 57 | 49 / 57 | 50 / 58 | 49 / 59 | 73 / 79 | 71 / 75 | 70 / 73 | 69 / 73 | 2.95 s |

Budget: warm p95 at most 25 ms slower than venv312 on both events. First-run times are reported, not judged against it.
Per-hook columns: 20-tool turn = UPS + 20×Pre + 20×Post + Stop, idle p50s; reported, not judged against the budget.
