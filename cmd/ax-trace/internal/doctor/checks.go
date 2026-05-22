package doctor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/manifest"
)

const otlpProbeTimeout = 5 * time.Second

// homeDir returns the home directory to use for filesystem lookups.
// When opts.HomeDir is set, it's used directly; otherwise the real $HOME
// is resolved.
func homeDir(opts Options) (string, error) {
	if opts.HomeDir != "" {
		return opts.HomeDir, nil
	}
	return os.UserHomeDir()
}

// expandHome rewrites a leading "~/" or "~" using the supplied home directory.
// Paths that don't begin with "~" are returned unchanged.
func expandHome(p, home string) string {
	if p == "" {
		return p
	}
	if p == "~" {
		return home
	}
	if strings.HasPrefix(p, "~/") {
		return filepath.Join(home, p[2:])
	}
	return p
}

// venvPython returns the venv python interpreter path under the given home.
// Mirrors paths.VenvPython but parameterizes the home so doctor checks can
// run against synthetic temp directories.
func venvPython(home string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(home, ".arize", "harness", "venv", "Scripts", "python.exe")
	}
	return filepath.Join(home, ".arize", "harness", "venv", "bin", "python")
}

// configFile returns ~/.arize/harness/config.yaml under the given home.
func configFile(home string) string {
	return filepath.Join(home, ".arize", "harness", "config.yaml")
}

// CheckVenv verifies the venv python interpreter exists at the platform-specific
// path under <home>/.arize/harness/venv. The interpreter is not executed — only
// its presence on disk is checked, so this check remains useful even when the
// venv is otherwise broken.
func CheckVenv(opts Options) Verdict {
	home, err := homeDir(opts)
	if err != nil {
		return Verdict{
			Name:      "venv",
			Pass:      false,
			Detail:    fmt.Sprintf("resolving home directory: %v", err),
			Remediate: "Set $HOME and re-run.",
		}
	}
	pyPath := venvPython(home)
	info, err := os.Stat(pyPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return Verdict{
				Name:      "venv",
				Pass:      false,
				Detail:    fmt.Sprintf("python interpreter missing at %s", pyPath),
				Remediate: "Run `ax-trace install` (or `./install.sh`) to bootstrap the venv.",
			}
		}
		return Verdict{
			Name:      "venv",
			Pass:      false,
			Detail:    fmt.Sprintf("stat %s: %v", pyPath, err),
			Remediate: "Check filesystem permissions on ~/.arize/harness/venv.",
		}
	}
	if info.IsDir() {
		return Verdict{
			Name:      "venv",
			Pass:      false,
			Detail:    fmt.Sprintf("expected file at %s, found directory", pyPath),
			Remediate: "Delete ~/.arize/harness/venv and re-run `ax-trace install`.",
		}
	}
	return Verdict{
		Name:   "venv",
		Pass:   true,
		Detail: fmt.Sprintf("python interpreter present at %s", pyPath),
	}
}

// CheckHarnessSettings verifies the harness's settings file exists and is
// parseable. JSON files are parsed; TOML files (used by Codex) are only
// checked for existence at v1. Harnesses without a configured settings_file
// are reported as a pass with an informational detail.
func CheckHarnessSettings(name string, entry manifest.HarnessEntry, opts Options) Verdict {
	checkName := fmt.Sprintf("settings:%s", name)
	if entry.SettingsFile == "" {
		return Verdict{
			Name:   checkName,
			Pass:   true,
			Detail: fmt.Sprintf("%s has no settings file in manifest; skipping", name),
		}
	}
	home, err := homeDir(opts)
	if err != nil {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("resolving home directory: %v", err),
			Remediate: "Set $HOME and re-run.",
		}
	}
	path := expandHome(entry.SettingsFile, home)
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return Verdict{
				Name:      checkName,
				Pass:      false,
				Detail:    fmt.Sprintf("settings file missing at %s", path),
				Remediate: fmt.Sprintf("Run `ax-trace install` to write %s settings, or launch %s once to create defaults.", entry.DisplayName, entry.DisplayName),
			}
		}
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("reading %s: %v", path, err),
			Remediate: "Check filesystem permissions on the settings file.",
		}
	}
	// Codex uses TOML; v1 doctor only verifies file existence for non-JSON
	// settings files. Detect by extension.
	if strings.EqualFold(filepath.Ext(path), ".toml") {
		return Verdict{
			Name:   checkName,
			Pass:   true,
			Detail: fmt.Sprintf("settings file present at %s (TOML parse not performed at v1)", path),
		}
	}
	var parsed any
	if err := json.Unmarshal(data, &parsed); err != nil {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("malformed JSON in %s: %v", path, err),
			Remediate: fmt.Sprintf("Open %s and fix the JSON syntax, or re-run `ax-trace install` to regenerate.", path),
		}
	}
	return Verdict{
		Name:   checkName,
		Pass:   true,
		Detail: fmt.Sprintf("settings file present and parseable at %s", path),
	}
}

// CheckHarnessEnv verifies that at least one of the harness's env keys is set
// (in the environment OR present in <home>/.arize/harness/config.yaml).
// Harnesses with no configured env keys are reported as a pass with an
// informational detail.
func CheckHarnessEnv(name string, entry manifest.HarnessEntry, opts Options) Verdict {
	checkName := fmt.Sprintf("env:%s", name)
	if len(entry.ArizeEnvKeys) == 0 {
		return Verdict{
			Name:   checkName,
			Pass:   true,
			Detail: fmt.Sprintf("%s declares no env keys in manifest; skipping", name),
		}
	}
	envHits := []string{}
	for _, key := range entry.ArizeEnvKeys {
		if _, ok := os.LookupEnv(key); ok {
			envHits = append(envHits, key)
		}
	}
	if len(envHits) > 0 {
		return Verdict{
			Name:   checkName,
			Pass:   true,
			Detail: fmt.Sprintf("env vars set in environment: %s", strings.Join(envHits, ", ")),
		}
	}
	home, err := homeDir(opts)
	if err != nil {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("resolving home directory: %v", err),
			Remediate: "Set $HOME and re-run.",
		}
	}
	cfgPath := configFile(home)
	configHits, cfgErr := configKeysPresent(cfgPath, entry.ArizeEnvKeys)
	if cfgErr != nil {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("reading %s: %v", cfgPath, cfgErr),
			Remediate: fmt.Sprintf("Fix YAML syntax in %s or remove it and re-run `ax-trace install`.", cfgPath),
		}
	}
	if len(configHits) > 0 {
		return Verdict{
			Name:   checkName,
			Pass:   true,
			Detail: fmt.Sprintf("env vars found in %s: %s", cfgPath, strings.Join(configHits, ", ")),
		}
	}
	return Verdict{
		Name:      checkName,
		Pass:      false,
		Detail:    fmt.Sprintf("none of %s are set (checked environment and %s)", strings.Join(entry.ArizeEnvKeys, ", "), cfgPath),
		Remediate: fmt.Sprintf("Run `ax-trace install` or `ax-trace config` to populate %s env vars.", name),
	}
}

// configKeysPresent reads cfgPath as YAML and returns the subset of env keys
// that appear as YAML mapping keys (case-sensitive). A missing file is not
// an error — it returns an empty result.
//
// To avoid a yaml dependency, we scan line by line for a key that, after
// trimming leading whitespace, is followed by ":". This avoids substring
// false positives like "MY_ARIZE_API_KEY" matching a search for
// "ARIZE_API_KEY", while still tolerating arbitrary nesting depth.
func configKeysPresent(cfgPath string, keys []string) ([]string, error) {
	data, err := os.ReadFile(cfgPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	lines := strings.Split(string(data), "\n")
	hits := []string{}
	for _, key := range keys {
		prefix := key + ":"
		for _, line := range lines {
			trimmed := strings.TrimLeft(line, " \t")
			if strings.HasPrefix(trimmed, prefix) {
				hits = append(hits, key)
				break
			}
		}
	}
	return hits, nil
}

// CheckOTLPEndpoint probes the configured OTLP endpoint with a HEAD request.
// Any response < 500 is considered healthy (Arize may return 4xx for HEAD,
// which still proves the endpoint is reachable). Times out after 5 seconds.
func CheckOTLPEndpoint(ctx context.Context, endpoint string, opts Options) Verdict {
	checkName := "otlp_endpoint"
	if endpoint == "" {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    "no OTLP endpoint configured",
			Remediate: "Set ARIZE_OTLP_ENDPOINT or restore the default in core/manifest.json.",
		}
	}
	probeURL := normalizeProbeURL(endpoint)
	client := opts.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: otlpProbeTimeout}
	}
	reqCtx, cancel := context.WithTimeout(ctx, otlpProbeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodHead, probeURL, nil)
	if err != nil {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("building request for %s: %v", probeURL, err),
			Remediate: fmt.Sprintf("Check that %s is a valid host:port or URL.", endpoint),
		}
	}
	resp, err := client.Do(req)
	if err != nil {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("HEAD %s: %v", probeURL, err),
			Remediate: "Verify network connectivity and that the endpoint is reachable from this machine.",
		}
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 500 {
		return Verdict{
			Name:      checkName,
			Pass:      false,
			Detail:    fmt.Sprintf("HEAD %s returned %d", probeURL, resp.StatusCode),
			Remediate: "The endpoint is reachable but returned a server error. Retry shortly or check status pages.",
		}
	}
	return Verdict{
		Name:   checkName,
		Pass:   true,
		Detail: fmt.Sprintf("HEAD %s returned %d (endpoint reachable)", probeURL, resp.StatusCode),
	}
}

// normalizeProbeURL converts a bare host:port (e.g. "otlp.arize.com:443") into
// an https:// URL. URLs that already specify a scheme pass through unchanged.
func normalizeProbeURL(endpoint string) string {
	if strings.Contains(endpoint, "://") {
		return endpoint
	}
	return "https://" + endpoint
}
