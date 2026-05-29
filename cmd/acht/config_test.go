package main

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

// -- pure helpers (no filesystem) ------------------------------------------

func TestGetDotted_NestedKey(t *testing.T) {
	root := map[string]any{
		"harnesses": map[string]any{
			"claude-code": map[string]any{
				"api_key": "sk-test",
			},
		},
	}
	got, ok := getDotted(root, "harnesses.claude-code.api_key")
	if !ok {
		t.Fatal("getDotted = !ok, want ok")
	}
	if got != "sk-test" {
		t.Errorf("getDotted = %v, want sk-test", got)
	}
}

func TestGetDotted_MissingKey(t *testing.T) {
	root := map[string]any{"a": map[string]any{"b": 1}}
	if _, ok := getDotted(root, "a.b.c"); ok {
		t.Error("getDotted returned ok for missing key")
	}
	if _, ok := getDotted(root, "x"); ok {
		t.Error("getDotted returned ok for missing top-level key")
	}
}

func TestSetDotted_CreatesIntermediateMaps(t *testing.T) {
	root := map[string]any{}
	if err := setDotted(root, "a.b.c", "v"); err != nil {
		t.Fatal(err)
	}
	got, _ := getDotted(root, "a.b.c")
	if got != "v" {
		t.Errorf("setDotted then getDotted = %v, want v", got)
	}
}

func TestSetDotted_ErrorsOnNonMapIntermediate(t *testing.T) {
	root := map[string]any{"a": "scalar"}
	if err := setDotted(root, "a.b", "v"); err == nil {
		t.Error("setDotted through non-map intermediate = nil, want error")
	}
}

func TestDeleteDotted_NoOpOnMissing(t *testing.T) {
	root := map[string]any{"a": 1}
	deleteDotted(root, "x.y.z")
	if root["a"] != 1 {
		t.Errorf("deleteDotted on missing key mutated root: %v", root)
	}
}

func TestDeleteDotted_RemovesLeaf(t *testing.T) {
	root := map[string]any{"a": map[string]any{"b": "v"}}
	deleteDotted(root, "a.b")
	inner, _ := root["a"].(map[string]any)
	if _, exists := inner["b"]; exists {
		t.Errorf("deleteDotted left key: %v", root)
	}
}

func TestParseScalar(t *testing.T) {
	cases := []struct {
		in   string
		want any
	}{
		{"true", true},
		{"false", false},
		{"42", float64(42)}, // JSON numbers come out as float64
		{"hello", "hello"},
		{`"42"`, "42"}, // explicit JSON string stays as string
		{"", ""},
	}
	for _, c := range cases {
		got := parseScalar(c.in)
		if got != c.want {
			t.Errorf("parseScalar(%q) = %v (%T), want %v (%T)", c.in, got, got, c.want, c.want)
		}
	}
}

func TestIsAPIKeyLeaf(t *testing.T) {
	cases := []struct {
		key  string
		want bool
	}{
		{"api_key", true},
		{"harnesses.claude-code.api_key", true},
		{"harnesses.claude-code.endpoint", false},
		{"api_key_extra", false},
		{"harnesses.api_key.foo", false},
	}
	for _, c := range cases {
		if got := isAPIKeyLeaf(c.key); got != c.want {
			t.Errorf("isAPIKeyLeaf(%q) = %v, want %v", c.key, got, c.want)
		}
	}
}

// -- masking ----------------------------------------------------------------

func TestMaskTree_MasksOnlyAPIKey(t *testing.T) {
	cfg := map[string]any{
		"verbose": true,
		"harnesses": map[string]any{
			"claude-code": map[string]any{
				"api_key":      "sk-secret-do-not-leak",
				"space_id":     "space_abc123",
				"project_name": "claude-code",
				"endpoint":     "otlp.arize.com:443",
			},
			"codex": map[string]any{
				"api_key": "sk-also-secret",
			},
		},
	}
	masked := maskTree(cfg)

	for _, harness := range []string{"claude-code", "codex"} {
		inner, _ := masked["harnesses"].(map[string]any)[harness].(map[string]any)
		if inner["api_key"] != maskedValue {
			t.Errorf("%s.api_key = %v, want %v", harness, inner["api_key"], maskedValue)
		}
	}
	claudeInner, _ := masked["harnesses"].(map[string]any)["claude-code"].(map[string]any)
	if claudeInner["space_id"] != "space_abc123" {
		t.Errorf("space_id was masked, should be visible: %v", claudeInner["space_id"])
	}
	if claudeInner["endpoint"] != "otlp.arize.com:443" {
		t.Errorf("endpoint was masked, should be visible: %v", claudeInner["endpoint"])
	}
	if masked["verbose"] != true {
		t.Errorf("verbose was changed: %v", masked["verbose"])
	}
}

func TestMaskTree_LeavesEmptyAPIKeyEmpty(t *testing.T) {
	cfg := map[string]any{
		"harnesses": map[string]any{
			"phoenix-thing": map[string]any{
				"api_key":  "",
				"endpoint": "http://localhost:6006",
			},
		},
	}
	masked := maskTree(cfg)
	inner, _ := masked["harnesses"].(map[string]any)["phoenix-thing"].(map[string]any)
	if inner["api_key"] != "" {
		t.Errorf("empty api_key was masked into %v; want stay empty", inner["api_key"])
	}
}

// -- end-to-end: show output never contains the secret ---------------------

func TestRunConfigShow_DefaultMasksSecret(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	// Write a config with a known api_key value.
	secret := "sk-do-not-print-in-test-output"
	cfg := map[string]any{
		"harnesses": map[string]any{
			"claude-code": map[string]any{
				"api_key": secret,
			},
		},
	}
	path := filepath.Join(tmp, ".arize", "harness", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := yaml.Marshal(cfg)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	// Capture stdout while runConfigShow runs.
	out := captureStdout(t, func() {
		if err := runConfigShow(false); err != nil {
			t.Fatal(err)
		}
	})

	if strings.Contains(out, secret) {
		t.Errorf("show output leaked secret (reveal=false): %s", out)
	}
	if !strings.Contains(out, maskedValue) {
		t.Errorf("show output missing masked marker %q: %s", maskedValue, out)
	}
}

func TestRunConfigShow_RevealShowsSecret(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	secret := "sk-reveal-me"
	cfg := map[string]any{
		"harnesses": map[string]any{
			"claude-code": map[string]any{"api_key": secret},
		},
	}
	path := filepath.Join(tmp, ".arize", "harness", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := yaml.Marshal(cfg)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	out := captureStdout(t, func() {
		if err := runConfigShow(true); err != nil {
			t.Fatal(err)
		}
	})

	if !strings.Contains(out, secret) {
		t.Errorf("--reveal show output missing secret: %s", out)
	}
}

func TestRunConfigGet_MasksAPIKey(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	secret := "sk-get-mask"
	cfg := map[string]any{
		"harnesses": map[string]any{
			"claude-code": map[string]any{"api_key": secret},
		},
	}
	path := filepath.Join(tmp, ".arize", "harness", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := yaml.Marshal(cfg)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	out := captureStdout(t, func() {
		if err := runConfigGet("harnesses.claude-code.api_key", false); err != nil {
			t.Fatal(err)
		}
	})
	if strings.Contains(out, secret) {
		t.Errorf("get without --reveal leaked secret: %s", out)
	}
	if !strings.Contains(out, maskedValue) {
		t.Errorf("get output missing masked marker: %s", out)
	}
}

// -- command registration ---------------------------------------------------

func TestConfigCommand_HasExpectedSubcommands(t *testing.T) {
	var configCmd *cobra.Command
	for _, cmd := range rootCmd.Commands() {
		if cmd.Use == "config" {
			configCmd = cmd
			break
		}
	}
	if configCmd == nil {
		t.Fatal("config command not registered")
	}
	want := []string{"get", "set", "delete", "show", "path", "edit"}
	got := map[string]bool{}
	for _, sub := range configCmd.Commands() {
		// Strip arg placeholders like "get <key>" → "get"
		name := strings.SplitN(sub.Use, " ", 2)[0]
		got[name] = true
	}
	for _, w := range want {
		if !got[w] {
			t.Errorf("config command missing subcommand %q", w)
		}
	}
}

// -- helpers ----------------------------------------------------------------

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	origStdout := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	defer func() { os.Stdout = origStdout }()

	done := make(chan struct{})
	var buf bytes.Buffer
	go func() {
		_, _ = io.Copy(&buf, r)
		close(done)
	}()

	fn()
	_ = w.Close()
	<-done
	return buf.String()
}
