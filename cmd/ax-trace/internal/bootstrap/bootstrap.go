// Package bootstrap ensures uv is installed, the repo is synced, and the
// venv exists — the prerequisites for dispatching into the Python wizards.
//
// All external commands run through a CommandRunner so tests can substitute a
// fake. The on-disk side effects (lock file, state file, repo extraction,
// venv creation) write under ~/.arize, which tests redirect via $HOME.
package bootstrap

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"runtime"
)

// CommandRunner abstracts subprocess execution so tests can replace it.
type CommandRunner interface {
	Run(ctx context.Context, name string, args []string, stdin io.Reader, stdout, stderr io.Writer, env []string) error
}

type realRunner struct{}

// Run executes name with the given arguments and streams.
func (realRunner) Run(ctx context.Context, name string, args []string, stdin io.Reader, stdout, stderr io.Writer, env []string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdin = stdin
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	if env != nil {
		cmd.Env = env
	}
	return cmd.Run()
}

// Options controls bootstrap behavior.
type Options struct {
	// Branch of the coding-harness-tracing repo to install/sync. Default "main".
	Branch string
	// Runner is the subprocess executor. Default realRunner{}.
	Runner CommandRunner
	// HTTPClient is used for installer / tarball downloads. Default http.DefaultClient.
	HTTPClient *http.Client
	// Stdout/Stderr receive bootstrap progress output. Defaults to os.Stdout/os.Stderr.
	Stdout io.Writer
	Stderr io.Writer
}

// Result describes what Bootstrap did.
type Result struct {
	UvPath     string
	VenvPython string
	Reused     bool
}

// withDefaults fills in zero-valued fields on opts. Returns the populated copy.
func withDefaults(opts Options) Options {
	if opts.Runner == nil {
		opts.Runner = realRunner{}
	}
	if opts.HTTPClient == nil {
		opts.HTTPClient = http.DefaultClient
	}
	if opts.Branch == "" {
		opts.Branch = "main"
	}
	if opts.Stdout == nil {
		opts.Stdout = os.Stdout
	}
	if opts.Stderr == nil {
		opts.Stderr = os.Stderr
	}
	return opts
}

// Bootstrap acquires the lock, ensures uv/repo/venv, and returns the venv
// Python path for the caller to exec.
func Bootstrap(ctx context.Context, opts Options) (*Result, error) {
	opts = withDefaults(opts)

	if err := AcquireLock(ctx); err != nil {
		return nil, err
	}
	defer func() { _ = ReleaseLock() }()

	uvPath, err := EnsureUv(ctx, opts)
	if err != nil {
		return nil, fmt.Errorf("ensuring uv: %w", err)
	}

	if err := EnsureRepo(ctx, opts); err != nil {
		return nil, fmt.Errorf("ensuring repo: %w", err)
	}

	venvPython, reused, err := EnsureVenv(ctx, opts, uvPath)
	if err != nil {
		return nil, fmt.Errorf("ensuring venv: %w", err)
	}

	// The SSL fix is applied once at venv creation. A reused venv already has
	// sitecustomize.py from its first bootstrap; re-applying on every run would
	// reinstall certifi for no benefit. Users on a venv that predates the fix
	// can `ax-trace uninstall` (or delete the venv dir) and reinstall.
	if runtime.GOOS == "darwin" && !reused {
		if err := EnsureMacOSSSLFix(ctx, opts, venvPython); err != nil {
			fmt.Fprintf(opts.Stderr, "warning: macOS SSL fix failed: %v\n", err)
		}
	}

	return &Result{UvPath: uvPath, VenvPython: venvPython, Reused: reused}, nil
}
