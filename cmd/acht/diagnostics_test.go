package main

import "testing"

func TestDiagnosticCommandsRegistered(t *testing.T) {
	want := []string{"doctor", "version", "config"}
	got := map[string]bool{}
	for _, cmd := range rootCmd.Commands() {
		got[cmd.Use] = true
	}
	for _, w := range want {
		if !got[w] {
			t.Errorf("expected command %q registered", w)
		}
	}
}
