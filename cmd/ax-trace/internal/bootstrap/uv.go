package bootstrap

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// These are vars (not consts) so tests in the bootstrap package can swap in
// httptest URLs. They are not part of the public API.
var (
	uvInstallURLUnix    = "https://astral.sh/uv/install.sh"
	uvInstallURLWindows = "https://astral.sh/uv/install.ps1"
)

// EnsureUv returns the path to uv, installing it if absent.
//
// Lookup order:
//  1. Cached path in state.json (if it still exists on disk)
//  2. uv on $PATH
//  3. ~/.local/bin/uv (the default install location for astral.sh's installer)
//  4. Run the official installer.
func EnsureUv(ctx context.Context, opts Options) (string, error) {
	opts = withDefaults(opts)

	if path, ok := lookupCachedUv(); ok {
		cacheUvPath(path)
		return path, nil
	}

	if path, err := exec.LookPath("uv"); err == nil {
		cacheUvPath(path)
		return path, nil
	}

	if path, ok := lookupLocalBinUv(); ok {
		cacheUvPath(path)
		return path, nil
	}

	fmt.Fprintln(opts.Stdout, "[ax-trace] uv not found — installing via the official installer")
	if err := installUvViaScript(ctx, opts); err != nil {
		return "", fmt.Errorf("installing uv: %w", err)
	}

	if path, err := exec.LookPath("uv"); err == nil {
		cacheUvPath(path)
		return path, nil
	}
	if path, ok := lookupLocalBinUv(); ok {
		cacheUvPath(path)
		return path, nil
	}
	return "", fmt.Errorf("uv installer succeeded but uv was not found on PATH or in ~/.local/bin")
}

// lookupCachedUv returns the cached uv path if it exists on disk.
func lookupCachedUv() (string, bool) {
	s, err := LoadState()
	if err != nil || s == nil || s.UvPath == "" {
		return "", false
	}
	if _, err := os.Stat(s.UvPath); err != nil {
		return "", false
	}
	return s.UvPath, true
}

// lookupLocalBinUv looks for uv at ~/.local/bin/uv (or .exe on Windows).
func lookupLocalBinUv() (string, bool) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", false
	}
	name := "uv"
	if runtime.GOOS == "windows" {
		name = "uv.exe"
	}
	candidate := filepath.Join(home, ".local", "bin", name)
	if _, err := os.Stat(candidate); err == nil {
		return candidate, true
	}
	return "", false
}

// cacheUvPath persists the discovered uv path. Errors are non-fatal: the
// caller can still proceed with the resolved path.
func cacheUvPath(path string) {
	s, err := LoadState()
	if err != nil {
		return
	}
	if s.UvPath == path {
		return
	}
	s.UvPath = path
	_ = SaveState(s)
}

// installUvViaScript downloads the official installer and pipes it to a shell.
//
// Unix: `sh` reading the script from stdin.
// Windows: `powershell -Command -` reading the script from stdin.
func installUvViaScript(ctx context.Context, opts Options) error {
	opts = withDefaults(opts)

	var url string
	var shell string
	var shellArgs []string
	switch runtime.GOOS {
	case "windows":
		url = uvInstallURLWindows
		shell = "powershell"
		shellArgs = []string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"}
	default:
		url = uvInstallURLUnix
		shell = "sh"
		shellArgs = nil
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("building installer request: %w", err)
	}
	resp, err := opts.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("downloading uv installer from %s: %w", url, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("downloading uv installer: HTTP %d from %s", resp.StatusCode, url)
	}

	script, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("reading uv installer body: %w", err)
	}

	return opts.Runner.Run(ctx, shell, shellArgs, bytes.NewReader(script), opts.Stdout, opts.Stderr, nil)
}
