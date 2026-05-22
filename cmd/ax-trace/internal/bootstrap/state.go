package bootstrap

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/paths"
)

// State is persisted to ~/.arize/ax-trace/state.json.
type State struct {
	UvPath             string `json:"uv_path,omitempty"`
	LastPackageVersion string `json:"last_package_version,omitempty"`
	LastSchemaVersion  int    `json:"last_schema_version,omitempty"`
}

// LoadState reads ~/.arize/ax-trace/state.json. A missing file yields an
// empty (non-nil) State and no error.
func LoadState() (*State, error) {
	path, err := paths.StateFile()
	if err != nil {
		return nil, fmt.Errorf("resolving state file path: %w", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return &State{}, nil
		}
		return nil, fmt.Errorf("reading state file %s: %w", path, err)
	}
	var s State
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("parsing state file %s: %w", path, err)
	}
	return &s, nil
}

// SaveState writes ~/.arize/ax-trace/state.json, creating the parent
// directory if necessary.
func SaveState(s *State) error {
	if s == nil {
		return errors.New("SaveState: nil state")
	}
	path, err := paths.StateFile()
	if err != nil {
		return fmt.Errorf("resolving state file path: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("creating state dir: %w", err)
	}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshaling state: %w", err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return fmt.Errorf("writing state file %s: %w", path, err)
	}
	return nil
}
