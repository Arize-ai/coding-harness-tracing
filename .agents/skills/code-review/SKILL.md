---
name: code-review
description: Use before opening a pull request to this repo. Reviews the working diff for correctness bugs and repo conventions, runs the test/lint gates, and can post findings directly on the PR. Triggers on "review my changes", "review this PR", "pre-PR review".
---

# Pre-PR Code Review

Review the current branch's changes against `main` for correctness, repo conventions, and gate compliance. Optionally post findings directly to the GitHub PR.

## How to Use This Skill

1. Determine the diff (see [Determine the Diff](#determine-the-diff))
2. Review the changes (see [Review for Correctness](#review-for-correctness) and [Check Repo Conventions](#check-repo-conventions))
3. Run the gates (see [Run the Gates](#run-the-gates))
4. Summarize findings (see [Summarize Findings](#summarize-findings))
5. Optionally post review comments to the PR (see [Posting Comments on the PR](#posting-comments-on-the-pr))

The default is **local-only / dry-run** — print findings, post nothing. Only post to the PR when the user explicitly asks.

## Determine the Diff

Compare the current branch against `main` and capture any uncommitted work:

```bash
git diff main...HEAD
git status
```

Use `git diff main...HEAD` (three-dot) to limit the diff to commits introduced on this branch — not unrelated commits that landed on `main` after the branch diverged. If `main` is not up to date locally, run `git fetch origin main` first and compare against `origin/main`.

If there is no diff against `main` and no uncommitted work, tell the user there is nothing to review and stop.

## Review for Correctness

Read the full diff and look for:

- **Logic bugs** — off-by-one errors, inverted conditionals, wrong default values, copy-paste mistakes between similar branches.
- **Error handling** — exceptions that are silently swallowed, missing cleanup in error paths, error messages that lose the original cause.
- **Edge cases** — empty inputs, missing keys/files, network/IO failures, concurrency, large inputs.
- **Resource handling** — file handles, sockets, subprocesses, and locks closed/released on every path.
- **Security** — untrusted input flowing into shell commands, paths, SQL, or HTTP requests; secrets leaking into logs or spans.

Be specific. Cite the file and line numbers; quote the offending lines when useful.

## Check Repo Conventions

This repo emits OpenTelemetry / OpenInference spans from AI coding-assistant harnesses to Arize AX or Phoenix. Shared logic lives in `core/`; each harness lives under `tracing/<harness>/`.

### New harness integrations

A new harness must mirror the existing layout used by `claude_code`, `codex`, `cursor`, `copilot`, `gemini`, and `kiro`:

- A `tracing/<harness>/` package (with hooks, span builders, and any harness-specific helpers).
- A `core/setup/<harness>.py` setup wizard.
- Console-script entry points registered in `pyproject.toml` under `[project.scripts]` (hooks and a `arize-setup-<harness>` wizard).

Flag any new harness that skips one of these.

### Python 3.9 floor

The repo targets `>=3.9`. CI runs on 3.9 through 3.14. Reject any 3.10+ syntax in the diff:

- No `import tomllib` (only stdlib in 3.11+) — use `tomli` if absolutely required, but prefer not introducing a parser at all.
- No `match`/`case` statements.
- No `X | Y` runtime unions (e.g. function annotations evaluated at runtime, `isinstance(x, int | str)`). Inside `from __future__ import annotations` modules they are stringified and safe, but flag any runtime use.
- No `list[int]` / `dict[str, int]` as runtime expressions without `from __future__ import annotations`.
- No other 3.10+ features (parenthesized context managers in `with` are 3.10+; structural pattern matching; etc.).

### No new runtime dependencies

The package is intended to install with zero PyPI runtime dependencies. Anything imported from `core/` or `tracing/` must be stdlib (or already-vendored). Flag any new entry under `[project] dependencies` in `pyproject.toml`. New `[project.optional-dependencies] dev = [...]` entries are allowed but should be justified.

## Run the Gates

Run both gates and capture their output. Report pass/fail for each.

```bash
uv run pytest tests/ -m "not slow"
uv run pre-commit run --all-files
```

If either fails, include the failing test names / hook names and the relevant error excerpts in the findings. Do not attempt to "fix" anything in this skill — the goal is to surface issues.

## Summarize Findings

Produce a single prioritized, actionable list. Group findings as:

1. **Blocking** — gate failures, correctness bugs, convention violations that must be fixed before merge.
2. **Suggested** — improvements, smaller risks, code-quality nits worth addressing.
3. **Optional** — style preferences and forward-looking ideas.

Each finding must be self-contained: file path, line number(s), a one-line description of the problem, and a concrete suggested change. If a finding spans multiple files, list each location.

## Posting Comments on the PR

**Default: print findings locally and post nothing.** Only post when the user explicitly asks ("post these to the PR", "leave these as review comments", etc.).

### Preconditions

1. `gh auth status` must succeed. If it does not, fall back to printing findings locally and tell the user that `gh` is unavailable and nothing was posted.
2. Identify the target PR. By default, look up the PR for the current branch:

   ```bash
   gh pr view --json number,headRefName
   ```

   If no PR exists for the branch, or the user passed an explicit PR number, use the value they provided. **Confirm the PR number and title with the user before posting anything.**

### Inline, diff-anchored comments (line-specific findings)

For findings tied to a specific line, post inline review comments via the REST API. Resolve `{owner}/{repo}` from `gh repo view --json nameWithOwner` (or the user's input):

```bash
gh api \
  --method POST \
  -H "Accept: application/vnd.github+json" \
  repos/{owner}/{repo}/pulls/{number}/comments \
  -f body="<finding body>" \
  -f commit_id="<head sha of the PR>" \
  -f path="<path/in/repo>" \
  -F line=<line number> \
  -f side="RIGHT"
```

Notes:

- `commit_id` should be the HEAD SHA of the PR (read from `gh pr view --json headRefOid -q .headRefOid`).
- `side` is `RIGHT` for the new version (added/modified lines), `LEFT` for the original. Use `RIGHT` for almost all findings on this-branch code.
- For multi-line ranges, set `start_line`, `start_side`, and `line`.
- One API call per finding.

### Summary review comment (non-line-specific findings)

For overall summary, gate results, and any findings not tied to a single line, post a single review comment:

```bash
gh pr review {number} --comment --body "<summary body>"
```

The summary should reference the inline comments by file/line so reviewers can navigate.

### After posting

Print a short confirmation listing how many inline comments were created and that the summary review was posted, including the PR URL (`gh pr view {number} --json url -q .url`).

## Dry-Run Mode (Default)

Unless the user has explicitly asked to post, do not call `gh api` or `gh pr review`. Print the same summary that would have been posted, prefixed with `[dry-run]`, and tell the user to re-run with "post to the PR" if they want the comments published.
