package main

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func setHome(t *testing.T, dir string) {
	t.Helper()
	t.Setenv("HOME", dir)
	if runtime.GOOS == "windows" {
		t.Setenv("USERPROFILE", dir)
	}
}

func TestParseHarnessList(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want []string
	}{
		{"empty", "", nil},
		{"newline only", "\n", nil},
		{"multiple blank lines", "\n\n\n", nil},
		{"single", "claude-code\n", []string{"claude-code"}},
		{"multiple", "claude-code\ncodex\ncursor\n", []string{"claude-code", "codex", "cursor"}},
		{"no trailing newline", "claude-code\ncodex", []string{"claude-code", "codex"}},
		{"whitespace trimmed", "  claude-code  \n\tcodex\t\n", []string{"claude-code", "codex"}},
		{"blank lines skipped", "claude-code\n\ncodex\n", []string{"claude-code", "codex"}},
		{"leading/trailing blanks", "\nclaude-code\n\n", []string{"claude-code"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := parseHarnessList(tc.in)
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("parseHarnessList(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

func TestHarnessSubdir(t *testing.T) {
	cases := []struct {
		key  string
		want string
	}{
		{"claude-code", "claude_code"},
		{"codex", "codex"},
		{"copilot", "copilot"},
		{"cursor", "cursor"},
		{"gemini", "gemini"},
		{"kiro", "kiro"},
		{"some-multi-dashed-key", "some_multi_dashed_key"},
		{"", ""},
	}
	for _, tc := range cases {
		t.Run(tc.key, func(t *testing.T) {
			got := harnessSubdir(tc.key)
			if got != tc.want {
				t.Errorf("harnessSubdir(%q) = %q, want %q", tc.key, got, tc.want)
			}
		})
	}
}

func TestVenvExists_FalseWhenMissing(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	if venvExists() {
		t.Error("venvExists() = true, want false when no venv on disk")
	}
}

func TestVenvExists_TrueWhenPresent(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	var pyPath string
	if runtime.GOOS == "windows" {
		pyPath = filepath.Join(tmp, ".arize", "harness", "venv", "Scripts", "python.exe")
	} else {
		pyPath = filepath.Join(tmp, ".arize", "harness", "venv", "bin", "python")
	}
	if err := os.MkdirAll(filepath.Dir(pyPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pyPath, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !venvExists() {
		t.Errorf("venvExists() = false, want true (python at %s)", pyPath)
	}
}

func TestRunUninstallSelected_NoVenv_ExitsZero(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	if err := runUninstallSelected(context.Background(), []string{"claude-code"}); err != nil {
		t.Errorf("runUninstallSelected with no venv = %v, want nil", err)
	}
}

func TestRunUninstallAll_NoVenv_RemovesInstallAndAxTraceDirs(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	installDir := filepath.Join(tmp, ".arize", "harness")
	axTraceHome := filepath.Join(tmp, ".arize", "ax-trace")
	for _, d := range []string{installDir, axTraceHome} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		marker := filepath.Join(d, "marker.txt")
		if err := os.WriteFile(marker, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	if err := runUninstallAll(context.Background()); err != nil {
		t.Fatalf("runUninstallAll with no venv = %v, want nil", err)
	}

	if _, err := os.Stat(installDir); !os.IsNotExist(err) {
		t.Errorf("install dir still exists after uninstall-all (err=%v)", err)
	}
	if _, err := os.Stat(axTraceHome); !os.IsNotExist(err) {
		t.Errorf("ax-trace home still exists after uninstall-all (err=%v)", err)
	}
}

func TestRunUninstallAll_NoVenv_NothingToRemove(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	if err := runUninstallAll(context.Background()); err != nil {
		t.Errorf("runUninstallAll on empty home = %v, want nil", err)
	}
}

func TestUpdateCommand_HasBranchFlag(t *testing.T) {
	var updateCmd, uninstallCmd bool
	for _, cmd := range rootCmd.Commands() {
		switch cmd.Use {
		case "update":
			updateCmd = true
			flag := cmd.Flag("branch")
			if flag == nil {
				t.Error("update command missing --branch flag")
				break
			}
			if flag.DefValue != "main" {
				t.Errorf("--branch default = %q, want %q", flag.DefValue, "main")
			}
		case "uninstall":
			uninstallCmd = true
		}
	}
	if !updateCmd {
		t.Error("update command not found")
	}
	if !uninstallCmd {
		t.Error("uninstall command not found")
	}
}

func TestUninstallCommand_RejectsPositionalArgs(t *testing.T) {
	var uninstallCmd interface {
		ValidateArgs([]string) error
	}
	for _, cmd := range rootCmd.Commands() {
		if strings.HasPrefix(cmd.Use, "uninstall") {
			uninstallCmd = cmd
			break
		}
	}
	if uninstallCmd == nil {
		t.Fatal("uninstall command not registered")
	}
	if err := uninstallCmd.ValidateArgs([]string{}); err != nil {
		t.Errorf("uninstall with 0 args = %v, want nil", err)
	}
	if err := uninstallCmd.ValidateArgs([]string{"claude-code"}); err == nil {
		t.Error("uninstall with positional arg = nil, want error (NoArgs)")
	}
}

func TestUninstallCommand_HasHarnessFlags(t *testing.T) {
	var uninstallCmd *cobra.Command
	for _, cmd := range rootCmd.Commands() {
		if strings.HasPrefix(cmd.Use, "uninstall") {
			uninstallCmd = cmd
			break
		}
	}
	if uninstallCmd == nil {
		t.Fatal("uninstall command not registered")
	}
	want := []string{"claude", "codex", "copilot", "cursor", "gemini", "kiro"}
	for _, name := range want {
		if uninstallCmd.Flag(name) == nil {
			t.Errorf("uninstall command missing --%s flag", name)
		}
	}
}
