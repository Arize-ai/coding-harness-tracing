package bootstrap

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/paths"
)

// macOS sitecustomize.py contents, copied verbatim from install.sh's
// _fix_macos_ssl_certs heredoc. Pointing Python's SSL stack at certifi's CA
// bundle prevents urllib failures against https://otlp.arize.com when running
// under the Python.org installer's bundled OpenSSL.
const macosSitecustomize = `# Arize Coding Harness Tracing: point Python's SSL stack at certifi's CA bundle on macOS.
# This runs automatically at interpreter startup, before any hook code.
import os as _os
try:
    import certifi as _certifi
    _bundle = _certifi.where()
    _os.environ.setdefault("SSL_CERT_FILE", _bundle)
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
except ImportError:
    pass
`

// EnsureVenv creates ~/.arize/harness/venv via uv if missing, then installs
// the package. Returns the path to venv's python and whether the venv was
// reused (existed and was healthy).
func EnsureVenv(ctx context.Context, opts Options, uvPath string) (venvPython string, reused bool, err error) {
	opts = withDefaults(opts)

	venvDir, err := paths.VenvDir()
	if err != nil {
		return "", false, fmt.Errorf("resolving venv dir: %w", err)
	}
	pyPath, err := paths.VenvPython()
	if err != nil {
		return "", false, fmt.Errorf("resolving venv python: %w", err)
	}
	installDir, err := paths.InstallDir()
	if err != nil {
		return "", false, fmt.Errorf("resolving install dir: %w", err)
	}

	if venvIsHealthy(ctx, opts, pyPath) {
		fmt.Fprintf(opts.Stdout, "[acht] reusing existing venv at %s\n", venvDir)
		return pyPath, true, nil
	}

	if err := os.MkdirAll(filepath.Dir(venvDir), 0o755); err != nil {
		return "", false, fmt.Errorf("creating venv parent dir: %w", err)
	}

	fmt.Fprintf(opts.Stdout, "[acht] creating venv at %s\n", venvDir)
	if err := opts.Runner.Run(ctx, uvPath,
		[]string{"venv", "--python", "3.9", venvDir},
		nil, opts.Stdout, opts.Stderr, nil); err != nil {
		return "", false, fmt.Errorf("uv venv: %w", err)
	}

	fmt.Fprintf(opts.Stdout, "[acht] installing coding-harness-tracing into venv\n")
	if err := opts.Runner.Run(ctx, uvPath,
		[]string{"pip", "install", "--python", pyPath, installDir},
		nil, opts.Stdout, opts.Stderr, nil); err != nil {
		return "", false, fmt.Errorf("uv pip install: %w", err)
	}

	return pyPath, false, nil
}

// venvIsHealthy returns true when the venv python responds to --version.
// Missing executable, runner failure, or non-zero exit all count as unhealthy.
func venvIsHealthy(ctx context.Context, opts Options, pyPath string) bool {
	if _, err := os.Stat(pyPath); err != nil {
		return false
	}
	var out bytes.Buffer
	err := opts.Runner.Run(ctx, pyPath, []string{"--version"}, nil, &out, &out, nil)
	return err == nil
}

// EnsureMacOSSSLFix installs certifi into the venv and writes a sitecustomize.py
// pointing Python's SSL stack at certifi's CA bundle. Mirrors install.sh's
// _fix_macos_ssl_certs. Non-fatal — callers log and continue on error.
func EnsureMacOSSSLFix(ctx context.Context, opts Options, venvPython string) error {
	opts = withDefaults(opts)

	if err := opts.Runner.Run(ctx, venvPython,
		[]string{"-m", "pip", "install", "--quiet", "certifi"},
		nil, opts.Stdout, opts.Stderr, nil); err != nil {
		return fmt.Errorf("installing certifi: %w", err)
	}

	var certifiOut bytes.Buffer
	if err := opts.Runner.Run(ctx, venvPython,
		[]string{"-c", "import certifi; print(certifi.where())"},
		nil, &certifiOut, opts.Stderr, nil); err != nil {
		return fmt.Errorf("locating certifi bundle: %w", err)
	}
	if strings.TrimSpace(certifiOut.String()) == "" {
		return fmt.Errorf("certifi.where() returned empty path")
	}

	var siteOut bytes.Buffer
	if err := opts.Runner.Run(ctx, venvPython,
		[]string{"-c", "import site; print(site.getsitepackages()[0])"},
		nil, &siteOut, opts.Stderr, nil); err != nil {
		return fmt.Errorf("locating site-packages: %w", err)
	}
	siteDir := strings.TrimSpace(siteOut.String())
	if siteDir == "" {
		return fmt.Errorf("site.getsitepackages() returned empty path")
	}

	sitecustomize := filepath.Join(siteDir, "sitecustomize.py")
	if err := os.MkdirAll(filepath.Dir(sitecustomize), 0o755); err != nil {
		return fmt.Errorf("creating site-packages dir: %w", err)
	}
	if err := os.WriteFile(sitecustomize, []byte(macosSitecustomize), 0o644); err != nil {
		return fmt.Errorf("writing sitecustomize.py: %w", err)
	}
	fmt.Fprintln(opts.Stdout, "[acht] SSL certificates configured via certifi")
	return nil
}
