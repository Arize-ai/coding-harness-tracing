# Contributor gate policies

Evaluate actual scope and evidence even if harness or size labels do not exist. Gate additions require sufficient issue text and comments, a concrete expected result, and a credible validation route. Triage-generated guesses do not make an underspecified issue sufficient.

## Shared decision path

1. Is work still needed? If fully present in the checkout, report evidence and release uncertainty; do not gate. If partly present, assess the remainder.
2. Is it available? Do not add a gate to assigned work or an issue with an open implementation PR or clearly active implementation branch. Leave existing gates on assignment alone; someone claiming a task is not criteria drift. `agent-in-progress` makes the entire issue read-only.
3. Is it startable? Reject missing requirements, explicit or prose blockers, unresolved design decisions, duplicates, epics, and security/privacy-sensitive work. Read comments for maintainer agreement on nontrivial changes, per `CONTRIBUTING.md`.
4. Is it contained? A new harness, broad shared-core redesign, or coordinated changes across harnesses is not a first issue. A focused core helper correction can qualify when callers and test expectations are clear.
5. Can a contributor verify it? Identify an offline test/fixture or a simple documentation verification. Unknown payload schemas or required access to a paid assistant/backend prevent a gate until an accessible validation route exists.
6. Apply only enabled policies with an existing matching label. If a policy label is missing, report eligibility and the missing label; do not create it.

## good-first-issue (default)

Label: `good first issue`.

Qualifies when all shared checks pass, the remaining work is S or a tightly bounded M, an existing implementation/test pattern provides a starting point, and the change is understandable without prior knowledge of the tracing lifecycle. A contained documentation correction can qualify. Mention the relevant source/test pattern in the report.

Do not gate unresolved race conditions, state-machine redesigns, credential routing, payload privacy changes, new integrations, or platform-specific failures with no accessible reproduction/fixture. Small diffs can still require deep expertise.

## good-student-issue (default)

Label: `good student issue`.

Qualifies when all shared checks pass and the issue offers a bounded coding project with clear learning value, acceptance criteria, source pointers, and accessible validation. S or M work can qualify; L qualifies only for depth within one component with an agreed approach, not a new harness, broad redesign, or coordination across integrations. A student project can involve understanding a parser or span-building algorithm beyond a typical first issue.

Require code or meaningful test work; pure documentation changes belong under `good first issue`. Exclude unresolved concurrency, credential/privacy behavior, inaccessible live-service testing, or tasks requiring unstated maintainer decisions. An issue may carry both human gates when it independently meets both policies. This policy is not tied to a particular course or calendar and does not promise a mentor assignment.

Calibration examples from this repository:

- A localized process-launch correction with one existing call site, a concrete visible failure, and an offline assertion of the launch options can take both human gates (the shape seen in #107).
- A transcript parser mismatch with a standalone reproduction, a named source line, a suggested established iteration pattern, and focused fixture coverage can take both human gates (the shape seen in #136).

These examples calibrate issue shape only. Re-read the live issue, assignments, linked PRs, and current default-branch code before applying a gate.

## good-agent-issue (explicit opt-in)

Label: `good-agent-issue`, only when it exists and the user explicitly enabled this policy and authorized the label write.

Require every shared check plus a precise testable result, a named existing pattern, deterministic offline tests, no architectural decision, and no access to credentials, paid services, or user-installed sessions. Inspect workflow consumers before applying the label and explain any implementation it starts. If the automation behavior cannot be established, propose the label locally instead.

Do not stack an agent assignment opportunity on an issue already advertised to humans. Prefer the existing human gate and report an eligible alternative; do not silently remove it to feed an agent queue. Do not add `agent-fix` or `agent-in-progress` as part of this policy.

## Regression

Re-evaluate existing gates within scope using the same criteria. Remove an enabled gate only for a clear, stated failure. Assignment alone, missing size/component labels, or an unavailable checkout is not evidence of failure. Read and compute missing evidence when possible; otherwise preserve the gate and report uncertainty. Never remove gates for policies the caller did not enable.
