// Package manifest reads core/manifest.json with a go:embed fallback.
//
// Resolution order:
//  1. ~/.arize/harness/core/manifest.json (installed repo copy) — preferred,
//     because it reflects the user's current install version.
//  2. Embedded copy from compile time.
//
// Schema version mismatches return an error; callers should suggest upgrading
// either ax-trace or running `ax-trace update`.
package manifest

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

const SupportedSchemaVersion = 1

type Manifest struct {
	SchemaVersion int                     `json:"schema_version"`
	Harnesses     map[string]HarnessEntry `json:"harnesses"`
	Shared        SharedEntry             `json:"shared"`
}

type HarnessEntry struct {
	DisplayName  string   `json:"display_name"`
	HarnessBin   string   `json:"harness_bin"`
	SettingsFile string   `json:"settings_file"`
	// HookEvents is deferred-parsed: some harnesses encode it as an array of
	// event names, others as an object mapping event name -> hook script.
	// Consumers that care about the contents can unmarshal it themselves.
	HookEvents   json.RawMessage `json:"hook_events"`
	ArizeEnvKeys []string        `json:"arize_env_keys"`
}

type SharedEntry struct {
	ConfigFile          string `json:"config_file"`
	InstallDir          string `json:"install_dir"`
	VenvDir             string `json:"venv_dir"`
	OtlpEndpointDefault string `json:"otlp_endpoint_default"`
}

// Load returns the manifest, preferring the installed-repo copy and falling
// back to the embedded copy.
func Load() (*Manifest, error) {
	if installed, err := installedManifestPath(); err == nil {
		if data, err := os.ReadFile(installed); err == nil {
			return parseManifest(data, installed)
		}
	}
	return parseManifest(embeddedManifest, "<embedded>")
}

// installedManifestPath returns the path to the manifest inside the user's
// installed-repo copy at ~/.arize/harness/core/manifest.json.
func installedManifestPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolving home directory: %w", err)
	}
	return filepath.Join(home, ".arize", "harness", "core", "manifest.json"), nil
}

func parseManifest(data []byte, source string) (*Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("parsing manifest at %s: %w", source, err)
	}
	if m.SchemaVersion != SupportedSchemaVersion {
		return nil, fmt.Errorf(
			"manifest schema %d not supported (ax-trace expects %d). Run `ax-trace update` or upgrade ax-trace",
			m.SchemaVersion, SupportedSchemaVersion,
		)
	}
	return &m, nil
}

// HarnessNames returns the list of harnesses in a stable, sorted order.
func (m *Manifest) HarnessNames() []string {
	names := make([]string, 0, len(m.Harnesses))
	for k := range m.Harnesses {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}
