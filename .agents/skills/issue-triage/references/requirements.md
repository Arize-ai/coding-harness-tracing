# Harness requirements

Use `CONTRIBUTING.md` and the affected harness's README/source as evidence. This repository builds spans from coding-assistant hooks and transcripts; it does not expose OpenInference instrumentor APIs such as `TraceConfig` or `suppress_tracing()` as universal requirements.

## Check the dimensions the issue touches

- **Privacy and configuration:** respect applicable prompt/tool logging controls, `ARIZE_TRACE_ENABLED`, dry-run behavior, and backend/config precedence. New paths must not bypass existing redaction or log credentials. Inspect `core/config.py` and the adapter rather than assuming every setting is used identically.
- **Span correctness:** identify expected OpenInference span kind, model identity, token usage, errors, timestamps, parent relationships, and session/user attributes. Specify only what the harness exposes; do not fabricate missing provider/model data. Check duplicate delivery, partial transcripts, tool correlation, or subagent boundaries when relevant.
- **Hook reliability:** tracing failures should not disrupt the assistant. Consider process boundaries, concurrent state updates, partial writes, shutdown behavior, and Windows encoding where the affected path needs them.
- **Compatibility:** Python 3.9 floor; hook runtime uses the standard library. `python-dotenv` is the installer-only dependency. Match the actual assistant event schema and supported OS/install path.
- **Installation:** new harnesses need the adapter/handlers/installer, setup wrapper, console entry points, shell and batch routing, status registration, tests, mypy hook, and README entry specified in `CONTRIBUTING.md`. Shared installer changes need consideration of existing user configuration.
- **Verification:** identify offline fixtures and focused tests demonstrating expected spans or registration behavior. Distinguish tests possible with synthetic payloads from validation requiring a licensed assistant, credentials, live backend, or particular OS.

## Checklist for additions

For new spans/attributes/integrations with missing acceptance criteria, select and specialize only the relevant rows below for the marked triage block. Preserve completed checks on later runs. Avoid turning triage into a speculative implementation design.

```markdown
**Requirements**

- [ ] State the hook/transcript input and expected span attributes and parent relationships.
- [ ] Honor applicable logging controls, trace-disable and dry-run behavior without exposing credentials.
- [ ] Keep hook processing compatible with Python 3.9 and the standard-library runtime.
- [ ] Cover missing/partial inputs and repeated events where applicable without disrupting the assistant.
- [ ] Add offline regression fixtures/tests for the affected behavior.
- [ ] For a new harness, complete the installation, status, routing, packaging, tests, and docs checklist in CONTRIBUTING.md.
```

For a small correction to an existing value, record the concrete expected value and test gap instead of appending this checklist. For documentation or CI issues, use their own acceptance criteria.
