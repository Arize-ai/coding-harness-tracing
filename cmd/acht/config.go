package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/paths"
)

// apiKeyLeafName is the YAML key whose values are masked in any output that
// could leak to a terminal scrollback, screen-share, or paste buffer.
// Only exact matches at the leaf of a dotted path are masked — adjacent
// non-secret fields like `space_id` and `endpoint` are shown as-is.
const apiKeyLeafName = "api_key"

const maskedValue = "***"

func init() {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Read or modify acht settings at ~/.arize/harness/config.yaml",
		Long: `Manage acht settings stored at ~/.arize/harness/config.yaml.

All operations work on the YAML file directly — no Python venv or repo
checkout is required. Dotted keys (harnesses.claude-code.api_key) traverse
nested mappings. API key values are masked by default; pass --reveal to
print them verbatim.`,
	}

	cmd.AddCommand(newConfigGetCmd())
	cmd.AddCommand(newConfigSetCmd())
	cmd.AddCommand(newConfigDeleteCmd())
	cmd.AddCommand(newConfigShowCmd())
	cmd.AddCommand(newConfigPathCmd())
	cmd.AddCommand(newConfigEditCmd())

	rootCmd.AddCommand(cmd)
}

func newConfigGetCmd() *cobra.Command {
	var reveal bool
	cmd := &cobra.Command{
		Use:   "get <key>",
		Short: "Print the value at a dotted key",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigGet(args[0], reveal)
		},
	}
	cmd.Flags().BoolVar(&reveal, "reveal", false, "Show api_key values in plaintext instead of masking them")
	return cmd
}

func newConfigSetCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "set <key> <value>",
		Short: "Set the value at a dotted key",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigSet(args[0], args[1])
		},
	}
	return cmd
}

func newConfigDeleteCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "delete <key>",
		Short: "Remove the value at a dotted key",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigDelete(args[0])
		},
	}
	return cmd
}

func newConfigShowCmd() *cobra.Command {
	var reveal bool
	cmd := &cobra.Command{
		Use:   "show",
		Short: "Print the entire config",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigShow(reveal)
		},
	}
	cmd.Flags().BoolVar(&reveal, "reveal", false, "Show api_key values in plaintext instead of masking them")
	return cmd
}

func newConfigPathCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "path",
		Short: "Print the config file path",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			path, err := paths.ConfigFile()
			if err != nil {
				return err
			}
			fmt.Println(path)
			return nil
		},
	}
}

func newConfigEditCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "edit",
		Short: "Open the config in $EDITOR",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfigEdit()
		},
	}
}

// runConfigGet prints the value at the requested dotted key, masking
// api_key leaves unless --reveal is set.
func runConfigGet(key string, reveal bool) error {
	cfg, _, err := loadConfig()
	if err != nil {
		return err
	}
	val, ok := getDotted(cfg, key)
	if !ok {
		return fmt.Errorf("key not found: %s", key)
	}
	if !reveal && isAPIKeyLeaf(key) {
		val = maskedValue
	}
	return writeYAML(os.Stdout, val)
}

// runConfigSet writes a value at the requested dotted key, creating
// intermediate mappings as needed. JSON-parseable values are stored as
// their typed equivalents (true/false/numbers); everything else is stored
// as a string.
func runConfigSet(key, raw string) error {
	cfg, path, err := loadConfig()
	if err != nil {
		return err
	}
	if cfg == nil {
		cfg = map[string]any{}
	}
	value := parseScalar(raw)
	if err := setDotted(cfg, key, value); err != nil {
		return err
	}
	return saveConfig(path, cfg)
}

// runConfigDelete removes the value at the requested dotted key. Missing
// keys are not an error — the operation is idempotent.
func runConfigDelete(key string) error {
	cfg, path, err := loadConfig()
	if err != nil {
		return err
	}
	if cfg == nil {
		return nil
	}
	deleteDotted(cfg, key)
	return saveConfig(path, cfg)
}

// runConfigShow prints the entire config with api_key values masked
// unless --reveal is set.
func runConfigShow(reveal bool) error {
	cfg, _, err := loadConfig()
	if err != nil {
		return err
	}
	if cfg == nil {
		fmt.Fprintln(os.Stdout, "(config file does not exist)")
		return nil
	}
	if !reveal {
		cfg = maskTree(cfg)
	}
	return writeYAML(os.Stdout, cfg)
}

// runConfigEdit launches $EDITOR against the config file. Creates the
// file and parent directory if either is missing — the editor opens an
// empty buffer the user can populate.
func runConfigEdit() error {
	path, err := paths.ConfigFile()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("creating parent dir for %s: %w", path, err)
	}
	if _, statErr := os.Stat(path); os.IsNotExist(statErr) {
		if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
			return fmt.Errorf("creating %s: %w", path, err)
		}
	}
	editor := os.Getenv("EDITOR")
	if editor == "" {
		editor = "vi"
	}
	c := exec.Command(editor, path)
	c.Stdin = os.Stdin
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	return c.Run()
}

// -- config file I/O --------------------------------------------------------

// loadConfig reads ~/.arize/harness/config.yaml and returns the parsed
// dict, the file path, and any error. A non-existent file returns
// (nil, path, nil) so callers can decide whether to create it on write.
func loadConfig() (map[string]any, string, error) {
	path, err := paths.ConfigFile()
	if err != nil {
		return nil, "", err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, path, nil
		}
		return nil, path, fmt.Errorf("reading %s: %w", path, err)
	}
	if len(data) == 0 {
		return map[string]any{}, path, nil
	}
	var raw any
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, path, fmt.Errorf("parsing %s: %w", path, err)
	}
	normalized := normalizeMap(raw)
	cfg, ok := normalized.(map[string]any)
	if !ok {
		return nil, path, fmt.Errorf("%s does not contain a YAML mapping at the top level", path)
	}
	return cfg, path, nil
}

// saveConfig writes the config map to disk, creating the parent
// directory if needed.
func saveConfig(path string, cfg map[string]any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("creating parent dir for %s: %w", path, err)
	}
	data, err := yaml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("encoding config: %w", err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	return nil
}

// writeYAML emits a value as YAML to the writer, with a trailing newline.
// Scalars are printed bare (no surrounding `key: ` wrapper).
func writeYAML(w *os.File, val any) error {
	switch v := val.(type) {
	case string:
		fmt.Fprintln(w, v)
		return nil
	case bool, int, int64, float64:
		fmt.Fprintf(w, "%v\n", v)
		return nil
	default:
		data, err := yaml.Marshal(v)
		if err != nil {
			return fmt.Errorf("encoding value: %w", err)
		}
		_, err = fmt.Fprint(w, string(data))
		return err
	}
}

// -- dotted-key traversal ---------------------------------------------------

// getDotted walks the config map by a dotted key path and returns the
// value at that path, or (nil, false) if any segment is missing.
func getDotted(root map[string]any, key string) (any, bool) {
	parts := strings.Split(key, ".")
	var cur any = root
	for _, p := range parts {
		m, ok := cur.(map[string]any)
		if !ok {
			return nil, false
		}
		cur, ok = m[p]
		if !ok {
			return nil, false
		}
	}
	return cur, true
}

// setDotted writes value at the dotted key path, creating intermediate
// mappings as needed. Returns an error if an intermediate path segment
// resolves to a non-mapping value (which would otherwise be silently
// overwritten).
func setDotted(root map[string]any, key string, value any) error {
	parts := strings.Split(key, ".")
	cur := root
	for i, p := range parts {
		if i == len(parts)-1 {
			cur[p] = value
			return nil
		}
		next, exists := cur[p]
		if !exists {
			m := map[string]any{}
			cur[p] = m
			cur = m
			continue
		}
		m, ok := next.(map[string]any)
		if !ok {
			return fmt.Errorf("cannot set %s: intermediate path %q is not a mapping", key, strings.Join(parts[:i+1], "."))
		}
		cur = m
	}
	return nil
}

// deleteDotted removes the value at the dotted key path. Missing keys
// are silently ignored.
func deleteDotted(root map[string]any, key string) {
	parts := strings.Split(key, ".")
	cur := root
	for i, p := range parts {
		if i == len(parts)-1 {
			delete(cur, p)
			return
		}
		next, exists := cur[p]
		if !exists {
			return
		}
		m, ok := next.(map[string]any)
		if !ok {
			return
		}
		cur = m
	}
}

// -- masking ----------------------------------------------------------------

// isAPIKeyLeaf returns true when the final segment of a dotted key is
// exactly "api_key".
func isAPIKeyLeaf(key string) bool {
	idx := strings.LastIndex(key, ".")
	if idx < 0 {
		return key == apiKeyLeafName
	}
	return key[idx+1:] == apiKeyLeafName
}

// maskTree returns a deep copy of cfg with every "api_key" leaf replaced
// by the masked placeholder. Empty/missing values stay empty so users
// can still tell which keys have anything set.
func maskTree(cfg map[string]any) map[string]any {
	out := make(map[string]any, len(cfg))
	for k, v := range cfg {
		out[k] = maskValue(k, v)
	}
	return out
}

func maskValue(key string, v any) any {
	switch x := v.(type) {
	case map[string]any:
		return maskTree(x)
	case []any:
		// We don't expect lists to contain api_key leaves under any current
		// schema, but defensively recurse so future schemas don't bypass the
		// mask.
		out := make([]any, len(x))
		for i, item := range x {
			out[i] = maskValue(key, item)
		}
		return out
	default:
		if key == apiKeyLeafName && !isZeroish(v) {
			return maskedValue
		}
		return v
	}
}

// isZeroish reports whether v should be treated as "not set" for masking
// purposes. Empty strings, nils, and zero numbers all qualify.
func isZeroish(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case string:
		return x == ""
	default:
		return false
	}
}

// -- value coercion ---------------------------------------------------------

// parseScalar best-effort-parses a CLI value string as JSON. This handles
// true/false, integers, floats, and quoted strings correctly. Anything
// else is returned as a plain string. The trade-off: the user has to
// quote string values that look like JSON literals (e.g. `set foo '"true"'`
// to store the literal string "true" instead of the boolean true).
func parseScalar(raw string) any {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return raw
	}
	var parsed any
	if err := json.Unmarshal([]byte(trimmed), &parsed); err == nil {
		return parsed
	}
	return raw
}

// -- normalize map[any]any → map[string]any --------------------------------

// normalizeMap walks a yaml.Unmarshal'd value and converts any
// map[any]any nodes (which yaml.v3 doesn't typically produce, but
// yaml.v2-compatible decoders can) into map[string]any. This keeps the
// rest of the code free of type-assertion forks.
func normalizeMap(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, val := range x {
			out[k] = normalizeMap(val)
		}
		return out
	case map[any]any:
		out := make(map[string]any, len(x))
		for k, val := range x {
			out[fmt.Sprintf("%v", k)] = normalizeMap(val)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, item := range x {
			out[i] = normalizeMap(item)
		}
		return out
	default:
		return v
	}
}
