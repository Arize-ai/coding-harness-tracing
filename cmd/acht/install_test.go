package main

import (
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestInstallCommandsRegistered(t *testing.T) {
	// The install commands live under `acht add <harness>`. Find the
	// `add` parent command first, then assert each harness is registered
	// under it.
	var addCmd *cobra.Command
	for _, cmd := range rootCmd.Commands() {
		if strings.HasPrefix(cmd.Use, "add") {
			addCmd = cmd
			break
		}
	}
	if addCmd == nil {
		t.Fatalf("expected `add` command registered on root, available: %v", strings.Join(commandNames(rootCmd), ","))
	}

	want := []string{"claude", "codex", "copilot", "cursor", "gemini", "kiro"}
	got := map[string]bool{}
	for _, sub := range addCmd.Commands() {
		got[sub.Use] = true
	}
	for _, w := range want {
		if !got[w] {
			t.Errorf("expected `add %s` subcommand registered, available: %v", w, strings.Join(commandNames(addCmd), ","))
		}
	}
}

func commandNames(c *cobra.Command) []string {
	out := []string{}
	for _, sub := range c.Commands() {
		out = append(out, sub.Use)
	}
	return out
}
