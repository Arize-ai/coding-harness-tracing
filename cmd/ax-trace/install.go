package main

// Each install subcommand (claude, codex, etc.) registers itself in init().
// Actual command logic is filled in by a later task.

func init() {
	// Placeholder root install group — actual per-harness commands are
	// registered by the install-commands task.
}
