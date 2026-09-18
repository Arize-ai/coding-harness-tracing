# Body formatting and publication

Read the complete current issue and comments before composing any change. Preserve reporter wording, code fences, stack traces, links, images, and details verbatim. A readable body needs no restructuring. Repair an unclear title without changing the reported claim or its template prefix.

The repository uses GitHub issue forms. When restructuring helps, retain their `###` headings:

- Bug: Harness; Backend; Version; What happened?; Reproduction steps; Additional context.
- Feature: Problem / motivation; Proposed solution; Alternatives considered; Additional context.

Use only headings supported by existing content. Leave coherent alternative structures alone. Never invent a reproduction or expected result to fill an empty field.

## Owned triage block

Append added guidance in one block:

```markdown
<!-- triage:begin -->
**Triage notes**

- Missing: the install method and the hook event that fails.

**Requirements**

- [ ] Add an offline fixture showing the expected parent/child span relationship.
<!-- triage:end -->
```

Re-derive this block from current evidence on each run. Remove resolved missing-information requests. Preserve completed checkboxes and human-added notes; change only rows whose meaning is clear. Requirements already documented outside the block do not need duplication. A single well-formed existing block is updated in place; never append a second block.

If markers are missing, duplicated, nested, or occur inside a quoted example/code fence, do not guess boundaries. Leave the body unchanged and report the problem. Do not overwrite a block whose human edits you cannot preserve. If no guidance remains, remove only the clearly owned obsolete content/block.

Do not edit bodies containing exposed credentials or personal session data. Report the kind and location without copying values. Do not quote that content in comments or logs.

## Write discipline

Only publish within the caller's authorization. Use a temporary file and `--body-file`, never inline shell interpolation of issue text. Immediately before submitting, re-read and compare the body/title and relevant state to the version used for composition; if it changed, recompute. GitHub issue edits lack an atomic compare-and-swap, so keep the read/write interval short and avoid bulk rewrites.

Read back the body/title after editing. On a mismatch, report the conflict instead of repeatedly overwriting. For comments, inspect existing `**Triage notes**` comments first; submit one combined note only when new actionable evidence warrants it and the caller authorized posting. A rerun with identical findings produces no comment.
