package bootstrap

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestWithDefaults_FillsZeroValues exercises the internal withDefaults helper
// to guard the contract used by every exported function in the package.
func TestWithDefaults_FillsZeroValues(t *testing.T) {
	got := withDefaults(Options{})
	if got.Runner == nil {
		t.Error("Runner should default to realRunner{} (non-nil)")
	}
	if got.HTTPClient == nil {
		t.Error("HTTPClient should default to http.DefaultClient (non-nil)")
	}
	if got.Branch != "main" {
		t.Errorf("Branch default = %q, want main", got.Branch)
	}
	if got.Stdout == nil || got.Stderr == nil {
		t.Error("Stdout/Stderr should be non-nil after withDefaults")
	}
}

// TestWithDefaults_PreservesProvidedValues verifies caller-provided options
// survive withDefaults.
func TestWithDefaults_PreservesProvidedValues(t *testing.T) {
	custom := &http.Client{}
	runner := &fakeRunner{}
	got := withDefaults(Options{
		Branch:     "feature/zzz",
		Runner:     runner,
		HTTPClient: custom,
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if got.Branch != "feature/zzz" {
		t.Errorf("Branch = %q, want feature/zzz", got.Branch)
	}
	if got.Runner != runner {
		t.Error("Runner should be preserved when explicitly set")
	}
	if got.HTTPClient != custom {
		t.Error("HTTPClient should be preserved when explicitly set")
	}
}

// TestEnsureRepo_BothFetchesFail confirms a non-nil error propagates when
// neither shallow nor full git fetch succeeds.
func TestEnsureRepo_BothFetchesFail(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	installDir := filepath.Join(tmp, ".arize", "harness")
	if err := os.MkdirAll(filepath.Join(installDir, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if c.Name == "git" {
				c.Err = errors.New("network down")
				return true
			}
			return false
		},
	}}
	err := EnsureRepo(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard})
	if err == nil {
		t.Fatal("expected error when all git fetch attempts fail")
	}
	// Verify both attempts (shallow + full) were made.
	gitCalls := runner.callsByName("git")
	if len(gitCalls) != 2 {
		t.Errorf("expected 2 git calls (shallow + full attempts), got %d", len(gitCalls))
	}
}

// TestEnsureRepo_TarballHTTPError propagates HTTP errors from the tarball
// download.
func TestEnsureRepo_TarballHTTPError(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	// .git absent, so tarball path is taken.

	failingClient := &http.Client{Transport: roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		return nil, errors.New("simulated network failure")
	})}

	runner := &fakeRunner{}
	err := EnsureRepo(context.Background(), Options{
		Runner:     runner,
		HTTPClient: failingClient,
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if err == nil {
		t.Fatal("expected error from failing HTTP client")
	}
	if !strings.Contains(err.Error(), "downloading tarball") {
		t.Errorf("error message %q should wrap context about tarball download", err.Error())
	}
}

type roundTripperFunc func(r *http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// TestEnsureMacOSSSLFix_EmptyCertifiPathErrors covers the guard against
// receiving an empty path from `certifi.where()`.
func TestEnsureMacOSSSLFix_EmptyCertifiPathErrors(t *testing.T) {
	setHome(t, t.TempDir())
	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			// Allow `python -m pip install certifi` to succeed.
			if len(c.Args) >= 2 && c.Args[0] == "-m" && c.Args[1] == "pip" {
				return true
			}
			// Return empty stdout for the certifi probe.
			if len(c.Args) >= 2 && c.Args[0] == "-c" && bytesContains(c.Args[1], "import certifi") {
				c.StdOut = "\n"
				return true
			}
			return false
		},
	}}
	err := EnsureMacOSSSLFix(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard}, "/fake/python")
	if err == nil {
		t.Fatal("expected error when certifi.where() returns empty")
	}
}

// TestBootstrap_OrchestratesAllStages exercises Bootstrap end-to-end with
// fakes for every subprocess. It covers the happy path and verifies the
// resulting venv python is reported and the macOS branch is non-fatal.
func TestBootstrap_OrchestratesAllStages(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	t.Cleanup(func() { _ = ReleaseLock() })

	// Pre-stage a cached uv path so EnsureUv short-circuits without an
	// installer download.
	uvBin := filepath.Join(tmp, "uv")
	if err := os.WriteFile(uvBin, []byte(""), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := SaveState(&State{UvPath: uvBin}); err != nil {
		t.Fatal(err)
	}

	// Pre-stage a `.git` dir so EnsureRepo uses git fetch (no network).
	installDir := filepath.Join(tmp, ".arize", "harness")
	if err := os.MkdirAll(filepath.Join(installDir, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}

	// Pre-stage venv python so EnsureVenv reuses (no `uv venv` call needed).
	venvBin := filepath.Join(installDir, "venv", "bin")
	if runtime.GOOS == "windows" {
		venvBin = filepath.Join(installDir, "venv", "Scripts")
	}
	if err := os.MkdirAll(venvBin, 0o755); err != nil {
		t.Fatal(err)
	}
	pyName := "python"
	if runtime.GOOS == "windows" {
		pyName = "python.exe"
	}
	pyPath := filepath.Join(venvBin, pyName)
	if err := os.WriteFile(pyPath, []byte(""), 0o755); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{}
	res, err := Bootstrap(context.Background(), Options{
		Runner: runner,
		Stdout: io.Discard,
		Stderr: io.Discard,
	})
	if err != nil {
		t.Fatalf("Bootstrap err = %v", err)
	}
	if res == nil {
		t.Fatal("Bootstrap returned nil result")
	}
	if res.UvPath != uvBin {
		t.Errorf("Result.UvPath = %q, want %q", res.UvPath, uvBin)
	}
	if res.VenvPython != pyPath {
		t.Errorf("Result.VenvPython = %q, want %q", res.VenvPython, pyPath)
	}
	if !res.Reused {
		t.Error("Result.Reused should be true when venv is healthy")
	}
}

// TestBootstrap_UvFailurePropagates ensures errors from any stage abort
// Bootstrap with a wrapped error.
func TestBootstrap_UvFailurePropagates(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	t.Cleanup(func() { _ = ReleaseLock() })

	// No cached uv, PATH empty, no ~/.local/bin/uv — installer path will run.
	t.Setenv("PATH", "")

	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			// Fail the installer subprocess unconditionally.
			c.Err = errors.New("installer exploded")
			return true
		},
	}}
	// Failing HTTP client short-circuits the installer download.
	failingClient := &http.Client{Transport: roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		return nil, errors.New("no network")
	})}

	_, err := Bootstrap(context.Background(), Options{
		Runner:     runner,
		HTTPClient: failingClient,
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if err == nil {
		t.Fatal("expected error from Bootstrap when uv installation fails")
	}
	if !strings.Contains(err.Error(), "ensuring uv") {
		t.Errorf("error %q should wrap 'ensuring uv'", err.Error())
	}
}
