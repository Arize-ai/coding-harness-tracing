package manifest

import (
	"strings"
	"testing"
)

func TestParseManifest_Good(t *testing.T) {
	data := []byte(`{
		"schema_version": 1,
		"harnesses": {
			"claude_code": {
				"display_name": "Claude Code",
				"harness_bin": "claude",
				"settings_file": "~/.claude/settings.json",
				"hook_events": ["SessionStart"],
				"arize_env_keys": ["ARIZE_API_KEY"]
			}
		},
		"shared": {
			"config_file": "~/.arize/harness/config.yaml",
			"install_dir": "~/.arize/harness",
			"venv_dir": "~/.arize/harness/venv",
			"otlp_endpoint_default": "otlp.arize.com:443"
		}
	}`)
	m, err := parseManifest(data, "test")
	if err != nil {
		t.Fatal(err)
	}
	if m.SchemaVersion != 1 {
		t.Errorf("schema = %d, want 1", m.SchemaVersion)
	}
	if got := m.Harnesses["claude_code"].DisplayName; got != "Claude Code" {
		t.Errorf("display_name = %q", got)
	}
}

func TestParseManifest_SchemaMismatch(t *testing.T) {
	data := []byte(`{"schema_version": 99, "harnesses": {}, "shared": {}}`)
	_, err := parseManifest(data, "test")
	if err == nil {
		t.Fatal("expected error for schema mismatch")
	}
	if !strings.Contains(err.Error(), "schema 99 not supported") {
		t.Errorf("error message = %q, want mention of schema 99", err.Error())
	}
}

func TestParseManifest_Malformed(t *testing.T) {
	_, err := parseManifest([]byte("not json"), "test")
	if err == nil {
		t.Fatal("expected error for malformed JSON")
	}
}

func TestEmbeddedManifest_IsValid(t *testing.T) {
	_, err := parseManifest(embeddedManifest, "<embedded>")
	if err != nil {
		t.Fatalf("embedded manifest is invalid: %v", err)
	}
}

func TestLoad_EmbeddedFallback(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	m, err := Load()
	if err != nil {
		t.Fatalf("Load() with no installed copy failed: %v", err)
	}
	if m == nil || len(m.Harnesses) == 0 {
		t.Fatal("Load() returned empty manifest from embedded fallback")
	}
}

func TestHarnessNames_Sorted(t *testing.T) {
	m := &Manifest{Harnesses: map[string]HarnessEntry{"zeta": {}, "alpha": {}, "mid": {}}}
	names := m.HarnessNames()
	want := []string{"alpha", "mid", "zeta"}
	for i, n := range want {
		if names[i] != n {
			t.Errorf("HarnessNames()[%d] = %q, want %q", i, names[i], n)
		}
	}
}
