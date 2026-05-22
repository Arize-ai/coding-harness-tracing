package doctor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/manifest"
)

// makeVenvPython lays down a synthetic python interpreter at the path
// CheckVenv expects under the given home directory.
func makeVenvPython(t *testing.T, home string) string {
	t.Helper()
	pyPath := venvPython(home)
	if err := os.MkdirAll(filepath.Dir(pyPath), 0o755); err != nil {
		t.Fatalf("mkdir venv: %v", err)
	}
	if err := os.WriteFile(pyPath, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write fake python: %v", err)
	}
	return pyPath
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir parent: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// unsetenv removes key from the environment for the duration of the test,
// restoring whatever value (or unset state) was present before.
//
// The t.Setenv-then-os.Unsetenv sequence is intentional: t.Setenv registers
// a cleanup that restores the original value at test end, but it also sets
// the key right now. We then immediately Unsetenv to clear it for the
// duration of the test body, while keeping the cleanup registered.
func unsetenv(t *testing.T, key string) {
	t.Helper()
	if original, ok := os.LookupEnv(key); ok {
		t.Setenv(key, original)
	}
	if err := os.Unsetenv(key); err != nil {
		t.Fatalf("unsetenv %s: %v", key, err)
	}
}

func TestCheckVenv_Pass(t *testing.T) {
	tmp := t.TempDir()
	makeVenvPython(t, tmp)
	v := CheckVenv(Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass, got fail: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, "python interpreter present") {
		t.Errorf("detail = %q, want mention of interpreter", v.Detail)
	}
}

func TestCheckVenv_Missing(t *testing.T) {
	tmp := t.TempDir()
	v := CheckVenv(Options{HomeDir: tmp})
	if v.Pass {
		t.Fatal("expected fail when python missing")
	}
	if !strings.Contains(v.Detail, "missing") {
		t.Errorf("detail = %q, want mention of missing", v.Detail)
	}
	if v.Remediate == "" {
		t.Error("expected non-empty remediation")
	}
}

func TestCheckHarnessSettings_Pass(t *testing.T) {
	tmp := t.TempDir()
	settingsPath := filepath.Join(tmp, ".claude", "settings.json")
	writeFile(t, settingsPath, `{"hooks": {}}`)
	entry := manifest.HarnessEntry{
		DisplayName:  "Claude Code",
		SettingsFile: "~/.claude/settings.json",
	}
	v := CheckHarnessSettings("claude_code", entry, Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass, got fail: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, settingsPath) {
		t.Errorf("detail = %q, want mention of path %s", v.Detail, settingsPath)
	}
}

func TestCheckHarnessSettings_Missing(t *testing.T) {
	tmp := t.TempDir()
	entry := manifest.HarnessEntry{
		DisplayName:  "Claude Code",
		SettingsFile: "~/.claude/settings.json",
	}
	v := CheckHarnessSettings("claude_code", entry, Options{HomeDir: tmp})
	if v.Pass {
		t.Fatal("expected fail when settings file missing")
	}
	expected := filepath.Join(tmp, ".claude", "settings.json")
	if !strings.Contains(v.Detail, expected) {
		t.Errorf("detail = %q, want mention of expected path %s", v.Detail, expected)
	}
}

func TestCheckHarnessSettings_Malformed(t *testing.T) {
	tmp := t.TempDir()
	settingsPath := filepath.Join(tmp, ".claude", "settings.json")
	writeFile(t, settingsPath, `{not valid json`)
	entry := manifest.HarnessEntry{
		DisplayName:  "Claude Code",
		SettingsFile: "~/.claude/settings.json",
	}
	v := CheckHarnessSettings("claude_code", entry, Options{HomeDir: tmp})
	if v.Pass {
		t.Fatal("expected fail for malformed JSON")
	}
	if !strings.Contains(v.Detail, "malformed JSON") {
		t.Errorf("detail = %q, want mention of malformed JSON", v.Detail)
	}
}

func TestCheckHarnessSettings_NoFileConfigured(t *testing.T) {
	tmp := t.TempDir()
	entry := manifest.HarnessEntry{
		DisplayName:  "Codex CLI",
		SettingsFile: "",
	}
	v := CheckHarnessSettings("codex", entry, Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass for harness with no settings file, got: %s", v.Detail)
	}
}

func TestCheckHarnessSettings_TOMLExistenceOnly(t *testing.T) {
	tmp := t.TempDir()
	tomlPath := filepath.Join(tmp, ".codex", "config.toml")
	// Garbage TOML content — v1 only checks existence for non-JSON files.
	writeFile(t, tomlPath, "this is not valid toml = = =")
	entry := manifest.HarnessEntry{
		DisplayName:  "Codex CLI",
		SettingsFile: "~/.codex/config.toml",
	}
	v := CheckHarnessSettings("codex", entry, Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass for TOML existence-only check, got: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, "TOML") {
		t.Errorf("detail = %q, want mention of TOML", v.Detail)
	}
}

func TestCheckHarnessEnv_EnvSet(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("ARIZE_API_KEY", "placeholder")
	unsetenv(t, "PHOENIX_API_KEY")
	entry := manifest.HarnessEntry{
		ArizeEnvKeys: []string{"ARIZE_API_KEY", "PHOENIX_API_KEY"},
	}
	v := CheckHarnessEnv("claude_code", entry, Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass when env var set, got: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, "ARIZE_API_KEY") {
		t.Errorf("detail = %q, want mention of ARIZE_API_KEY", v.Detail)
	}
}

func TestCheckHarnessEnv_ConfigSet(t *testing.T) {
	tmp := t.TempDir()
	unsetenv(t, "ARIZE_API_KEY")
	cfgPath := configFile(tmp)
	// Reference the key name without using a credential-shaped value.
	writeFile(t, cfgPath, "harnesses:\n  claude_code:\n    ARIZE_API_KEY: placeholder\n")
	entry := manifest.HarnessEntry{
		ArizeEnvKeys: []string{"ARIZE_API_KEY"},
	}
	v := CheckHarnessEnv("claude_code", entry, Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass when key present in config.yaml, got: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, cfgPath) {
		t.Errorf("detail = %q, want mention of config path", v.Detail)
	}
}

func TestCheckHarnessEnv_NeitherSet(t *testing.T) {
	tmp := t.TempDir()
	unsetenv(t, "ARIZE_API_KEY")
	entry := manifest.HarnessEntry{
		ArizeEnvKeys: []string{"ARIZE_API_KEY"},
	}
	v := CheckHarnessEnv("claude_code", entry, Options{HomeDir: tmp})
	if v.Pass {
		t.Fatal("expected fail when neither env nor config has key")
	}
	if !strings.Contains(v.Detail, "ARIZE_API_KEY") {
		t.Errorf("detail = %q, want mention of key name", v.Detail)
	}
	if v.Remediate == "" {
		t.Error("expected non-empty remediation")
	}
}

func TestCheckHarnessEnv_NoKeysConfigured(t *testing.T) {
	tmp := t.TempDir()
	entry := manifest.HarnessEntry{ArizeEnvKeys: nil}
	v := CheckHarnessEnv("cursor", entry, Options{HomeDir: tmp})
	if !v.Pass {
		t.Fatalf("expected pass when harness declares no env keys, got: %s", v.Detail)
	}
}

func TestCheckOTLPEndpoint_OK(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	v := CheckOTLPEndpoint(context.Background(), srv.URL, Options{HTTPClient: srv.Client()})
	if !v.Pass {
		t.Fatalf("expected pass for 200, got: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, "200") {
		t.Errorf("detail = %q, want mention of 200", v.Detail)
	}
}

func TestCheckOTLPEndpoint_MethodNotAllowed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusMethodNotAllowed)
	}))
	defer srv.Close()
	v := CheckOTLPEndpoint(context.Background(), srv.URL, Options{HTTPClient: srv.Client()})
	if !v.Pass {
		t.Fatalf("expected pass for 405 (endpoint reachable), got: %s", v.Detail)
	}
	if !strings.Contains(v.Detail, "405") {
		t.Errorf("detail = %q, want mention of 405", v.Detail)
	}
}

func TestCheckOTLPEndpoint_ServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	v := CheckOTLPEndpoint(context.Background(), srv.URL, Options{HTTPClient: srv.Client()})
	if v.Pass {
		t.Fatal("expected fail for 500")
	}
	if !strings.Contains(v.Detail, "500") {
		t.Errorf("detail = %q, want mention of 500", v.Detail)
	}
}

func TestCheckOTLPEndpoint_Unreachable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL
	srv.Close() // close immediately so connection is refused
	v := CheckOTLPEndpoint(context.Background(), url, Options{HTTPClient: srv.Client()})
	if v.Pass {
		t.Fatal("expected fail for unreachable endpoint")
	}
	if v.Remediate == "" {
		t.Error("expected non-empty remediation")
	}
}

func TestCheckOTLPEndpoint_NormalizesBareHostPort(t *testing.T) {
	got := normalizeProbeURL("otlp.arize.com:443")
	if !strings.HasPrefix(got, "https://") {
		t.Errorf("normalizeProbeURL(host:port) = %q, want https:// prefix", got)
	}
	got = normalizeProbeURL("http://example.com")
	if got != "http://example.com" {
		t.Errorf("normalizeProbeURL(url) = %q, want unchanged", got)
	}
}

func TestRun_AllHarnesses(t *testing.T) {
	tmp := t.TempDir()
	// Set HOME so manifest.Load() falls back to the embedded copy instead of
	// reading an installed manifest from the developer's real home.
	t.Setenv("HOME", tmp)
	if runtime.GOOS == "windows" {
		t.Setenv("USERPROFILE", tmp)
	}
	makeVenvPython(t, tmp)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	verdicts, err := Run(context.Background(), Options{
		HomeDir:      tmp,
		HTTPClient:   srv.Client(),
		OTLPEndpoint: srv.URL,
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	// Expect at least: venv + (settings + env) per harness + otlp.
	if len(verdicts) < 4 {
		t.Errorf("expected several verdicts, got %d", len(verdicts))
	}
	names := map[string]bool{}
	sawSettings, sawEnv := false, false
	for _, v := range verdicts {
		names[v.Name] = true
		if strings.HasPrefix(v.Name, "settings:") {
			sawSettings = true
		}
		if strings.HasPrefix(v.Name, "env:") {
			sawEnv = true
		}
	}
	if !names["venv"] {
		t.Error("missing venv check in verdicts")
	}
	if !names["otlp_endpoint"] {
		t.Error("missing otlp_endpoint check in verdicts")
	}
	if !sawSettings {
		t.Error("expected at least one settings:<harness> verdict from harness loop")
	}
	if !sawEnv {
		t.Error("expected at least one env:<harness> verdict from harness loop")
	}
}

func TestCheckOTLPEndpoint_EmptyEndpoint(t *testing.T) {
	v := CheckOTLPEndpoint(context.Background(), "", Options{})
	if v.Pass {
		t.Fatal("expected fail for empty endpoint")
	}
	if !strings.Contains(v.Detail, "no OTLP endpoint configured") {
		t.Errorf("detail = %q, want mention of missing endpoint", v.Detail)
	}
	if v.Remediate == "" {
		t.Error("expected non-empty remediation")
	}
}

func TestConfigKeysPresent_RejectsSubstringFalsePositive(t *testing.T) {
	tmp := t.TempDir()
	cfgPath := configFile(tmp)
	writeFile(t, cfgPath, "harnesses:\n  claude_code:\n    MY_ARIZE_API_KEY: placeholder\n")
	hits, err := configKeysPresent(cfgPath, []string{"ARIZE_API_KEY"})
	if err != nil {
		t.Fatalf("configKeysPresent: %v", err)
	}
	if len(hits) != 0 {
		t.Errorf("expected no hits for substring-only match, got %v", hits)
	}
}

func TestConfigKeysPresent_AcceptsNestedYAMLKey(t *testing.T) {
	tmp := t.TempDir()
	cfgPath := configFile(tmp)
	writeFile(t, cfgPath, "harnesses:\n  claude_code:\n    ARIZE_API_KEY: placeholder\n")
	hits, err := configKeysPresent(cfgPath, []string{"ARIZE_API_KEY"})
	if err != nil {
		t.Fatalf("configKeysPresent: %v", err)
	}
	if len(hits) != 1 || hits[0] != "ARIZE_API_KEY" {
		t.Errorf("expected single ARIZE_API_KEY hit, got %v", hits)
	}
}

func TestExpandHome(t *testing.T) {
	home := filepath.Join("nonexistent", "home")
	cases := []struct {
		in, want string
	}{
		{"~", home},
		{"~/x/y", filepath.Join(home, "x", "y")},
		{"/abs/path", "/abs/path"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := expandHome(tc.in, home); got != tc.want {
			t.Errorf("expandHome(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestVenvPython_PlatformSpecific(t *testing.T) {
	home := t.TempDir()
	got := venvPython(home)
	if runtime.GOOS == "windows" {
		if !strings.HasSuffix(got, filepath.Join("Scripts", "python.exe")) {
			t.Errorf("venvPython = %q, want Scripts/python.exe suffix", got)
		}
	} else {
		if !strings.HasSuffix(got, filepath.Join("bin", "python")) {
			t.Errorf("venvPython = %q, want bin/python suffix", got)
		}
	}
}
