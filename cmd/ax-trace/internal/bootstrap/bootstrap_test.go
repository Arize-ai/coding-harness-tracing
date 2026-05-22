package bootstrap

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
)

// -- fake runner -------------------------------------------------------------

type fakeCall struct {
	Name   string
	Args   []string
	StdIn  []byte
	StdOut string
	StdErr string
	Err    error
}

// fakeRunner records calls and consults handlers for canned behavior.
type fakeRunner struct {
	mu       sync.Mutex
	Calls    []fakeCall
	Handlers []func(call *fakeCall) (matched bool)
}

func (f *fakeRunner) Run(ctx context.Context, name string, args []string, stdin io.Reader, stdout, stderr io.Writer, env []string) error {
	call := fakeCall{Name: name, Args: append([]string(nil), args...)}
	if stdin != nil {
		b, _ := io.ReadAll(stdin)
		call.StdIn = b
	}
	for _, h := range f.Handlers {
		if h(&call) {
			break
		}
	}
	if stdout != nil && call.StdOut != "" {
		_, _ = stdout.Write([]byte(call.StdOut))
	}
	if stderr != nil && call.StdErr != "" {
		_, _ = stderr.Write([]byte(call.StdErr))
	}
	f.mu.Lock()
	f.Calls = append(f.Calls, call)
	f.mu.Unlock()
	return call.Err
}

func (f *fakeRunner) callsByName(name string) []fakeCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []fakeCall
	for _, c := range f.Calls {
		if c.Name == name {
			out = append(out, c)
		}
	}
	return out
}

// setHome sets HOME (and USERPROFILE on Windows) to dir for the test.
func setHome(t *testing.T, dir string) {
	t.Helper()
	t.Setenv("HOME", dir)
	if runtime.GOOS == "windows" {
		t.Setenv("USERPROFILE", dir)
	}
}

// -- State -------------------------------------------------------------------

func TestLoadState_Missing(t *testing.T) {
	setHome(t, t.TempDir())
	s, err := LoadState()
	if err != nil {
		t.Fatalf("LoadState() err = %v, want nil", err)
	}
	if s == nil {
		t.Fatal("LoadState() returned nil for missing file")
	}
	if s.UvPath != "" {
		t.Errorf("UvPath = %q, want empty", s.UvPath)
	}
}

func TestSaveLoadState_Roundtrip(t *testing.T) {
	setHome(t, t.TempDir())
	in := &State{UvPath: "/opt/uv", LastPackageVersion: "1.2.3", LastSchemaVersion: 1}
	if err := SaveState(in); err != nil {
		t.Fatalf("SaveState err = %v", err)
	}
	out, err := LoadState()
	if err != nil {
		t.Fatalf("LoadState err = %v", err)
	}
	if out.UvPath != in.UvPath || out.LastPackageVersion != in.LastPackageVersion || out.LastSchemaVersion != in.LastSchemaVersion {
		t.Errorf("roundtrip mismatch: got %+v, want %+v", out, in)
	}
}

func TestLoadState_MalformedReturnsError(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	dir := filepath.Join(tmp, ".arize", "ax-trace")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "state.json"), []byte("not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadState(); err == nil {
		t.Fatal("LoadState() with malformed JSON expected error, got nil")
	}
}

// -- EnsureUv ----------------------------------------------------------------

func TestEnsureUv_UsesCachedPath(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	uvBin := filepath.Join(tmp, "uv-bin")
	if err := os.WriteFile(uvBin, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := SaveState(&State{UvPath: uvBin}); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{}
	got, err := EnsureUv(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard})
	if err != nil {
		t.Fatalf("EnsureUv err = %v", err)
	}
	if got != uvBin {
		t.Errorf("EnsureUv = %q, want cached path %q", got, uvBin)
	}
	if len(runner.Calls) != 0 {
		t.Errorf("expected no subprocess calls; got %d", len(runner.Calls))
	}
}

func TestEnsureUv_CachedPathStaleFallsBackToLocalBin(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	// Cache references a path that no longer exists.
	stale := filepath.Join(tmp, "missing-uv")
	if err := SaveState(&State{UvPath: stale}); err != nil {
		t.Fatal(err)
	}

	// Create ~/.local/bin/uv so lookupLocalBinUv finds it.
	localBin := filepath.Join(tmp, ".local", "bin")
	if err := os.MkdirAll(localBin, 0o755); err != nil {
		t.Fatal(err)
	}
	uvName := "uv"
	if runtime.GOOS == "windows" {
		uvName = "uv.exe"
	}
	uvBin := filepath.Join(localBin, uvName)
	if err := os.WriteFile(uvBin, []byte(""), 0o755); err != nil {
		t.Fatal(err)
	}

	// Hide system uv so exec.LookPath misses it.
	t.Setenv("PATH", "")

	runner := &fakeRunner{}
	got, err := EnsureUv(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard})
	if err != nil {
		t.Fatalf("EnsureUv err = %v", err)
	}
	if got != uvBin {
		t.Errorf("EnsureUv = %q, want ~/.local/bin uv %q", got, uvBin)
	}
}

func TestEnsureUv_InvokesInstaller(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("installer subprocess shape differs on windows; covered by Unix happy-path")
	}
	tmp := t.TempDir()
	setHome(t, tmp)
	t.Setenv("PATH", "")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "#!/bin/sh\necho fake uv installer\n")
	}))
	defer server.Close()
	oldURL := uvInstallURLUnix
	uvInstallURLUnix = server.URL
	defer func() { uvInstallURLUnix = oldURL }()

	// Drop a fake uv into ~/.local/bin so post-install lookup succeeds.
	localBin := filepath.Join(tmp, ".local", "bin")
	if err := os.MkdirAll(localBin, 0o755); err != nil {
		t.Fatal(err)
	}
	expected := filepath.Join(localBin, "uv")

	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if c.Name != "sh" {
				return false
			}
			// Simulate installer creating ~/.local/bin/uv.
			if err := os.WriteFile(expected, []byte("#!/bin/sh\n"), 0o755); err != nil {
				c.Err = err
			}
			return true
		},
	}}

	got, err := EnsureUv(context.Background(), Options{
		Runner:     runner,
		HTTPClient: server.Client(),
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if err != nil {
		t.Fatalf("EnsureUv err = %v", err)
	}
	if got != expected {
		t.Errorf("EnsureUv = %q, want %q", got, expected)
	}
	sh := runner.callsByName("sh")
	if len(sh) != 1 {
		t.Fatalf("expected 1 sh invocation, got %d", len(sh))
	}
	if !bytes.Contains(sh[0].StdIn, []byte("fake uv installer")) {
		t.Errorf("installer script not piped via stdin; got %q", sh[0].StdIn)
	}
}

// EnsureUv's installer path runs against a custom HTTPClient. Point it at a
// server that returns a non-2xx code and confirm the error propagates.
func TestEnsureUv_InstallerHTTPError(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	t.Setenv("PATH", "")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()
	oldUnix, oldWin := uvInstallURLUnix, uvInstallURLWindows
	uvInstallURLUnix = server.URL
	uvInstallURLWindows = server.URL
	defer func() { uvInstallURLUnix = oldUnix; uvInstallURLWindows = oldWin }()

	runner := &fakeRunner{}
	_, err := EnsureUv(context.Background(), Options{
		Runner:     runner,
		HTTPClient: server.Client(),
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if err == nil {
		t.Fatal("EnsureUv expected error from HTTP 500, got nil")
	}
	if len(runner.callsByName("sh")) != 0 || len(runner.callsByName("powershell")) != 0 {
		t.Errorf("installer shell should not be invoked after HTTP failure")
	}
}

// EnsureUv calls installUvViaScript through the runner, so we can test
// against the runner directly to confirm shape per-platform.
func TestEnsureUv_InstallerRunnerFailurePropagates(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("non-Windows runner shape test")
	}
	tmp := t.TempDir()
	setHome(t, tmp)
	t.Setenv("PATH", "")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "#!/bin/sh\n")
	}))
	defer server.Close()
	oldURL := uvInstallURLUnix
	uvInstallURLUnix = server.URL
	defer func() { uvInstallURLUnix = oldURL }()

	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if c.Name == "sh" {
				c.Err = errors.New("simulated installer failure")
				return true
			}
			return false
		},
	}}
	_, err := EnsureUv(context.Background(), Options{
		Runner:     runner,
		HTTPClient: server.Client(),
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if err == nil {
		t.Fatal("expected error when installer subprocess fails")
	}
}

// -- EnsureRepo --------------------------------------------------------------

func TestEnsureRepo_GitFetchAndCheckout(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	installDir := filepath.Join(tmp, ".arize", "harness")
	if err := os.MkdirAll(filepath.Join(installDir, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{}
	err := EnsureRepo(context.Background(), Options{
		Branch: "feature/x",
		Runner: runner,
		Stdout: io.Discard,
		Stderr: io.Discard,
	})
	if err != nil {
		t.Fatalf("EnsureRepo err = %v", err)
	}
	gitCalls := runner.callsByName("git")
	if len(gitCalls) < 2 {
		t.Fatalf("expected at least 2 git calls (fetch + checkout), got %d", len(gitCalls))
	}
	if gitCalls[len(gitCalls)-1].Args[len(gitCalls[len(gitCalls)-1].Args)-1] != "FETCH_HEAD" {
		t.Errorf("last git call should be checkout FETCH_HEAD; got %v", gitCalls[len(gitCalls)-1].Args)
	}
	// Verify the fetch arg list includes the branch.
	found := false
	for _, c := range gitCalls {
		for _, a := range c.Args {
			if a == "feature/x" {
				found = true
			}
		}
	}
	if !found {
		t.Errorf("expected branch name 'feature/x' in git args; got calls %+v", gitCalls)
	}
}

func TestEnsureRepo_ShallowFetchFallsBackToFull(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	installDir := filepath.Join(tmp, ".arize", "harness")
	if err := os.MkdirAll(filepath.Join(installDir, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}

	shallowFailed := false
	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if c.Name != "git" {
				return false
			}
			for _, a := range c.Args {
				if a == "--depth" {
					c.Err = errors.New("shallow fetch unsupported")
					shallowFailed = true
					return true
				}
			}
			return false
		},
	}}

	err := EnsureRepo(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard})
	if err != nil {
		t.Fatalf("EnsureRepo err = %v", err)
	}
	if !shallowFailed {
		t.Error("expected the shallow fetch attempt to be made")
	}
	// Should have invoked: shallow fetch (fails), full fetch (ok), checkout (ok).
	if got := len(runner.callsByName("git")); got != 3 {
		t.Errorf("expected 3 git calls (shallow+full+checkout), got %d", got)
	}
}

func TestEnsureRepo_TarballExtract(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	tarBytes := makeTarball(t, map[string]string{
		"coding-harness-tracing-main/README.md":          "hello",
		"coding-harness-tracing-main/core/manifest.json": "{}",
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/gzip")
		_, _ = w.Write(tarBytes)
	}))
	defer server.Close()
	oldURL := repoTarballURLFormat
	// %s consumes the branch; we point the entire URL at the test server.
	repoTarballURLFormat = server.URL + "/branch-%s.tgz"
	defer func() { repoTarballURLFormat = oldURL }()

	runner := &fakeRunner{}
	err := EnsureRepo(context.Background(), Options{
		Runner:     runner,
		HTTPClient: server.Client(),
		Stdout:     io.Discard,
		Stderr:     io.Discard,
	})
	if err != nil {
		t.Fatalf("EnsureRepo err = %v", err)
	}
	if len(runner.callsByName("git")) != 0 {
		t.Errorf("git should not be invoked when .git is absent; got %d calls", len(runner.callsByName("git")))
	}
	// Verify the tarball was extracted to ~/.arize/harness with the top-level
	// component stripped.
	got, err := os.ReadFile(filepath.Join(tmp, ".arize", "harness", "README.md"))
	if err != nil {
		t.Fatalf("extracted README missing: %v", err)
	}
	if string(got) != "hello" {
		t.Errorf("README contents = %q, want hello", got)
	}
}

// makeTarball builds an in-memory .tar.gz with the given file map.
func makeTarball(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for name, body := range files {
		hdr := &tar.Header{
			Name:     name,
			Mode:     0o644,
			Size:     int64(len(body)),
			Typeflag: tar.TypeReg,
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// Test extractTarGz directly — it's deterministic and side-effect-isolated.
func TestExtractTarGz_StripsFirstComponent(t *testing.T) {
	tmp := t.TempDir()
	data := makeTarball(t, map[string]string{
		"top/README.md":          "hello",
		"top/sub/nested.txt":     "world",
		"top/core/manifest.json": "{}",
	})
	if err := extractTarGz(bytes.NewReader(data), tmp); err != nil {
		t.Fatalf("extractTarGz err = %v", err)
	}
	want := map[string]string{
		"README.md":          "hello",
		"sub/nested.txt":     "world",
		"core/manifest.json": "{}",
	}
	for rel, body := range want {
		got, err := os.ReadFile(filepath.Join(tmp, rel))
		if err != nil {
			t.Errorf("missing %s: %v", rel, err)
			continue
		}
		if string(got) != body {
			t.Errorf("%s = %q, want %q", rel, got, body)
		}
	}
}

func TestExtractTarGz_RejectsTraversal(t *testing.T) {
	tmp := t.TempDir()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	body := []byte("pwned")
	hdr := &tar.Header{
		Name:     "top/../../escape.txt",
		Mode:     0o644,
		Size:     int64(len(body)),
		Typeflag: tar.TypeReg,
	}
	if err := tw.WriteHeader(hdr); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(body); err != nil {
		t.Fatal(err)
	}
	_ = tw.Close()
	_ = gz.Close()

	err := extractTarGz(bytes.NewReader(buf.Bytes()), tmp)
	if err == nil {
		t.Fatal("expected error for path traversal entry")
	}
}

func TestExtractTarGz_RejectsUnsafeSymlinkTarget(t *testing.T) {
	cases := []struct {
		name    string
		linkTo  string
		wantErr bool
	}{
		{"absolute target", "/etc/passwd", true},
		{"traversal target", "../../../etc/passwd", true},
		{"safe relative target", "sibling.txt", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tmp := t.TempDir()
			var buf bytes.Buffer
			gz := gzip.NewWriter(&buf)
			tw := tar.NewWriter(gz)
			if err := tw.WriteHeader(&tar.Header{
				Name:     "top/link",
				Linkname: tc.linkTo,
				Typeflag: tar.TypeSymlink,
				Mode:     0o644,
			}); err != nil {
				t.Fatal(err)
			}
			_ = tw.Close()
			_ = gz.Close()

			err := extractTarGz(bytes.NewReader(buf.Bytes()), tmp)
			if tc.wantErr {
				if err == nil {
					t.Fatal("expected error for unsafe symlink target")
				}
			} else if err != nil {
				t.Fatalf("unexpected error for safe target %q: %v", tc.linkTo, err)
			}
		})
	}
}

func TestStripFirstComponent(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"top/file.txt", "file.txt"},
		{"top/sub/file.txt", "sub/file.txt"},
		{"top", ""},
		{"./top/file.txt", "file.txt"},
		{"top/", ""},
	}
	for _, c := range cases {
		if got := stripFirstComponent(c.in); got != c.want {
			t.Errorf("stripFirstComponent(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// -- EnsureVenv --------------------------------------------------------------

func TestEnsureVenv_ReusesHealthyVenv(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	installDir := filepath.Join(tmp, ".arize", "harness")
	venvDir := filepath.Join(installDir, "venv")
	binDir := filepath.Join(venvDir, "bin")
	if runtime.GOOS == "windows" {
		binDir = filepath.Join(venvDir, "Scripts")
	}
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	pyName := "python"
	if runtime.GOOS == "windows" {
		pyName = "python.exe"
	}
	pyPath := filepath.Join(binDir, pyName)
	if err := os.WriteFile(pyPath, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{}
	got, reused, err := EnsureVenv(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard}, "/path/to/uv")
	if err != nil {
		t.Fatalf("EnsureVenv err = %v", err)
	}
	if !reused {
		t.Error("expected reused=true when python --version succeeds")
	}
	if got != pyPath {
		t.Errorf("EnsureVenv path = %q, want %q", got, pyPath)
	}
	// Only one call expected: python --version.
	if len(runner.Calls) != 1 {
		t.Errorf("expected 1 runner call (python --version); got %d", len(runner.Calls))
	}
	for _, c := range runner.callsByName("/path/to/uv") {
		t.Errorf("uv should not be invoked for healthy venv; got call %+v", c)
	}
}

func TestEnsureVenv_CreatesWhenVersionCheckFails(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	uvPath := "/fake/uv"
	installDir := filepath.Join(tmp, ".arize", "harness")
	if err := os.MkdirAll(installDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// Pre-create the python binary so the stat passes, but have the runner
	// return a non-zero exit for `python --version` so venvIsHealthy is false.
	venvDir := filepath.Join(installDir, "venv")
	binDir := filepath.Join(venvDir, "bin")
	if runtime.GOOS == "windows" {
		binDir = filepath.Join(venvDir, "Scripts")
	}
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	pyName := "python"
	if runtime.GOOS == "windows" {
		pyName = "python.exe"
	}
	pyPath := filepath.Join(binDir, pyName)
	if err := os.WriteFile(pyPath, []byte(""), 0o755); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if len(c.Args) > 0 && c.Args[0] == "--version" {
				c.Err = errors.New("broken venv")
				return true
			}
			return false
		},
	}}

	got, reused, err := EnsureVenv(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard}, uvPath)
	if err != nil {
		t.Fatalf("EnsureVenv err = %v", err)
	}
	if reused {
		t.Error("expected reused=false when python --version fails")
	}
	if got != pyPath {
		t.Errorf("EnsureVenv path = %q, want %q", got, pyPath)
	}

	uvCalls := runner.callsByName(uvPath)
	if len(uvCalls) != 2 {
		t.Fatalf("expected 2 uv calls (venv + pip install), got %d", len(uvCalls))
	}
	if uvCalls[0].Args[0] != "venv" {
		t.Errorf("first uv call args[0] = %q, want venv", uvCalls[0].Args[0])
	}
	if uvCalls[1].Args[0] != "pip" || uvCalls[1].Args[1] != "install" {
		t.Errorf("second uv call args = %v, want pip install ...", uvCalls[1].Args)
	}
}

func TestEnsureVenv_PropagatesUvFailure(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	if err := os.MkdirAll(filepath.Join(tmp, ".arize", "harness"), 0o755); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if len(c.Args) > 0 && c.Args[0] == "venv" {
				c.Err = errors.New("uv venv broke")
				return true
			}
			return false
		},
	}}

	_, _, err := EnsureVenv(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard}, "uv")
	if err == nil {
		t.Fatal("expected error when uv venv fails")
	}
}

// -- EnsureMacOSSSLFix -------------------------------------------------------

func TestEnsureMacOSSSLFix_WritesSiteCustomize(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	siteDir := filepath.Join(tmp, "site-packages")
	if err := os.MkdirAll(siteDir, 0o755); err != nil {
		t.Fatal(err)
	}

	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if len(c.Args) >= 2 && c.Args[0] == "-c" {
				switch {
				case bytesContains(c.Args[1], "import certifi"):
					c.StdOut = "/some/certifi/cacert.pem\n"
					return true
				case bytesContains(c.Args[1], "site.getsitepackages"):
					c.StdOut = siteDir + "\n"
					return true
				}
			}
			return false
		},
	}}

	err := EnsureMacOSSSLFix(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard}, "/fake/python")
	if err != nil {
		t.Fatalf("EnsureMacOSSSLFix err = %v", err)
	}

	got, err := os.ReadFile(filepath.Join(siteDir, "sitecustomize.py"))
	if err != nil {
		t.Fatalf("sitecustomize.py not written: %v", err)
	}
	if !bytes.Contains(got, []byte("SSL_CERT_FILE")) {
		t.Errorf("sitecustomize.py missing SSL_CERT_FILE; got: %s", got)
	}
}

func TestEnsureMacOSSSLFix_PipFailureReturnsError(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	runner := &fakeRunner{Handlers: []func(*fakeCall) bool{
		func(c *fakeCall) bool {
			if len(c.Args) >= 2 && c.Args[0] == "-m" && c.Args[1] == "pip" {
				c.Err = errors.New("certifi install failed")
				return true
			}
			return false
		},
	}}
	err := EnsureMacOSSSLFix(context.Background(), Options{Runner: runner, Stdout: io.Discard, Stderr: io.Discard}, "/fake/python")
	if err == nil {
		t.Fatal("expected error when pip install certifi fails")
	}
}

func bytesContains(s, sub string) bool {
	return bytes.Contains([]byte(s), []byte(sub))
}

// -- Lock --------------------------------------------------------------------

func TestAcquireLock_AndRelease(t *testing.T) {
	setHome(t, t.TempDir())
	t.Cleanup(func() { _ = ReleaseLock() })
	ctx := context.Background()
	if err := AcquireLock(ctx); err != nil {
		t.Fatalf("AcquireLock err = %v", err)
	}
	if err := ReleaseLock(); err != nil {
		t.Fatalf("ReleaseLock err = %v", err)
	}
	// Second acquire/release should work fine.
	if err := AcquireLock(ctx); err != nil {
		t.Fatalf("AcquireLock (2nd) err = %v", err)
	}
	if err := ReleaseLock(); err != nil {
		t.Fatalf("ReleaseLock (2nd) err = %v", err)
	}
}

func TestAcquireLock_DoubleAcquireFailsFast(t *testing.T) {
	setHome(t, t.TempDir())
	t.Cleanup(func() { _ = ReleaseLock() })
	ctx := context.Background()
	if err := AcquireLock(ctx); err != nil {
		t.Fatalf("AcquireLock err = %v", err)
	}
	if err := AcquireLock(ctx); err == nil {
		t.Fatal("expected error on second AcquireLock from same process")
	}
}

// -- Tarball URL formatting --------------------------------------------------

func TestTarballURLBranchSubstitution(t *testing.T) {
	got := fmt.Sprintf(repoTarballURLFormat, "feature/x")
	want := "https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/feature/x.tar.gz"
	if got != want {
		t.Errorf("tarball url = %q, want %q", got, want)
	}
}

// Roundtripping State via raw JSON ensures the field tags are correct.
func TestState_JSONFieldTags(t *testing.T) {
	s := State{UvPath: "/u", LastPackageVersion: "v", LastSchemaVersion: 1}
	data, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{`"uv_path":"/u"`, `"last_package_version":"v"`, `"last_schema_version":1`}
	for _, w := range want {
		if !bytes.Contains(data, []byte(w)) {
			t.Errorf("JSON %s missing %s", data, w)
		}
	}
}
