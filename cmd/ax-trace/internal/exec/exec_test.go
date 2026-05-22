package exec

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func setHome(t *testing.T, dir string) {
	t.Helper()
	t.Setenv("HOME", dir)
	if runtime.GOOS == "windows" {
		t.Setenv("USERPROFILE", dir)
	}
}

// installFakeBin places a copy of (or hardlink to) the test binary at the
// path the Dispatch venv-resolver expects. The result is that when Dispatch
// runs <name>, it ends up re-executing this same test binary — which then
// branches into TestHelperProcess via GO_WANT_HELPER_PROCESS=1.
func installFakeBin(t *testing.T, home, name string) string {
	t.Helper()
	var venvBinDir, binName string
	if runtime.GOOS == "windows" {
		venvBinDir = filepath.Join(home, ".arize", "harness", "venv", "Scripts")
		binName = name + ".exe"
	} else {
		venvBinDir = filepath.Join(home, ".arize", "harness", "venv", "bin")
		binName = name
	}
	if err := os.MkdirAll(venvBinDir, 0o755); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(venvBinDir, binName)
	if err := os.Link(os.Args[0], dest); err != nil {
		src, oerr := os.Open(os.Args[0])
		if oerr != nil {
			t.Fatalf("open test binary: %v", oerr)
		}
		defer src.Close()
		dst, cerr := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
		if cerr != nil {
			t.Fatalf("create fake bin: %v", cerr)
		}
		defer dst.Close()
		if _, cperr := io.Copy(dst, src); cperr != nil {
			t.Fatalf("copy test binary: %v", cperr)
		}
	}
	return dest
}

func helperEnv(extra map[string]string) map[string]string {
	env := map[string]string{"GO_WANT_HELPER_PROCESS": "1"}
	for k, v := range extra {
		env[k] = v
	}
	return env
}

// TestHelperProcess is not a real test. When Dispatch tests re-exec the test
// binary as a fake venv entrypoint, this function controls its behavior via
// HELPER_* env vars. It exits the process directly so the testing framework's
// summary line never reaches the parent's captured stdout.
func TestHelperProcess(t *testing.T) {
	if os.Getenv("GO_WANT_HELPER_PROCESS") != "1" {
		return
	}
	if want := os.Getenv("HELPER_PRINT_ENV"); want != "" {
		fmt.Println(os.Getenv(want))
	}
	code, _ := strconv.Atoi(os.Getenv("HELPER_EXIT"))
	os.Exit(code)
}

func TestBuildInstallEnv_AllNil_ReturnsEmpty(t *testing.T) {
	got := BuildInstallEnv(InstallEnv{})
	if len(got) != 0 {
		t.Errorf("BuildInstallEnv with all-nil = %v, want empty map", got)
	}
}

func TestBuildInstallEnv_AllSet(t *testing.T) {
	backend := "arize"
	spaceID := "U3BhY2U6MTIz"
	otlp := "https://otlp.example.com"
	phoenix := "https://phoenix.example.com"
	project := "my-project"
	user := "user@example.com"
	logPrompts := true
	logToolDetails := false
	logToolContent := true
	verbose := false

	got := BuildInstallEnv(InstallEnv{
		Backend:         &backend,
		SpaceID:         &spaceID,
		OTLPEndpoint:    &otlp,
		PhoenixEndpoint: &phoenix,
		ProjectName:     &project,
		UserID:          &user,
		LogPrompts:      &logPrompts,
		LogToolDetails:  &logToolDetails,
		LogToolContent:  &logToolContent,
		Verbose:         &verbose,
		NonInteractive:  true,
	})

	want := map[string]string{
		"ARIZE_INSTALL_BACKEND":          "arize",
		"ARIZE_INSTALL_SPACE_ID":         "U3BhY2U6MTIz",
		"ARIZE_INSTALL_OTLP_ENDPOINT":    "https://otlp.example.com",
		"ARIZE_INSTALL_PHOENIX_ENDPOINT": "https://phoenix.example.com",
		"ARIZE_INSTALL_PROJECT_NAME":     "my-project",
		"ARIZE_INSTALL_USER_ID":          "user@example.com",
		"ARIZE_INSTALL_LOG_PROMPTS":      "true",
		"ARIZE_INSTALL_LOG_TOOL_DETAILS": "false",
		"ARIZE_INSTALL_LOG_TOOL_CONTENT": "true",
		"ARIZE_INSTALL_VERBOSE":          "false",
		"ARIZE_INSTALL_NON_INTERACTIVE":  "1",
	}
	if len(got) != len(want) {
		t.Fatalf("BuildInstallEnv map size = %d, want %d (got=%v)", len(got), len(want), got)
	}
	for k, wv := range want {
		gv, ok := got[k]
		if !ok {
			t.Errorf("BuildInstallEnv missing key %q", k)
			continue
		}
		if gv != wv {
			t.Errorf("BuildInstallEnv[%q] = %q, want %q", k, gv, wv)
		}
	}
}

func TestBuildInstallEnv_NonInteractiveAlone(t *testing.T) {
	got := BuildInstallEnv(InstallEnv{NonInteractive: true})
	if v, ok := got["ARIZE_INSTALL_NON_INTERACTIVE"]; !ok || v != "1" {
		t.Errorf("ARIZE_INSTALL_NON_INTERACTIVE = %q, want %q", v, "1")
	}
	if len(got) != 1 {
		t.Errorf("NonInteractive-only map size = %d, want 1 (got=%v)", len(got), got)
	}
}

func TestBoolStr(t *testing.T) {
	if got := boolStr(true); got != "true" {
		t.Errorf("boolStr(true) = %q, want %q", got, "true")
	}
	if got := boolStr(false); got != "false" {
		t.Errorf("boolStr(false) = %q, want %q", got, "false")
	}
}

func TestDispatch_ExitsZero(t *testing.T) {
	home := t.TempDir()
	setHome(t, home)
	installFakeBin(t, home, "fakebin")

	code, err := Dispatch(context.Background(), DispatchOptions{
		BinName: "fakebin",
		Args:    []string{"-test.run=^TestHelperProcess$"},
		Env:     helperEnv(map[string]string{"HELPER_EXIT": "0"}),
		Stdout:  io.Discard,
		Stderr:  io.Discard,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if code != 0 {
		t.Errorf("Dispatch exit code = %d, want 0", code)
	}
}

func TestDispatch_PropagatesNonZeroExit(t *testing.T) {
	home := t.TempDir()
	setHome(t, home)
	installFakeBin(t, home, "fakebin")

	code, err := Dispatch(context.Background(), DispatchOptions{
		BinName: "fakebin",
		Args:    []string{"-test.run=^TestHelperProcess$"},
		Env:     helperEnv(map[string]string{"HELPER_EXIT": "7"}),
		Stdout:  io.Discard,
		Stderr:  io.Discard,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if code != 7 {
		t.Errorf("Dispatch exit code = %d, want 7", code)
	}
}

func TestDispatch_MissingBinaryReturnsError(t *testing.T) {
	home := t.TempDir()
	setHome(t, home)

	code, err := Dispatch(context.Background(), DispatchOptions{
		BinName: "nonexistent",
		Stdout:  io.Discard,
		Stderr:  io.Discard,
	})
	if err == nil {
		t.Fatalf("Dispatch on missing binary returned nil error (code=%d)", code)
	}
	if code != -1 {
		t.Errorf("Dispatch missing binary code = %d, want -1", code)
	}
	if !strings.Contains(err.Error(), "venv binary not found") {
		t.Errorf("error %q does not mention 'venv binary not found'", err.Error())
	}
}

func TestDispatch_PassesEnvVarsToChild(t *testing.T) {
	home := t.TempDir()
	setHome(t, home)
	installFakeBin(t, home, "fakebin")

	var stdout bytes.Buffer
	code, err := Dispatch(context.Background(), DispatchOptions{
		BinName: "fakebin",
		Args:    []string{"-test.run=^TestHelperProcess$"},
		Env: helperEnv(map[string]string{
			"HELPER_EXIT":           "0",
			"HELPER_PRINT_ENV":      "ARIZE_INSTALL_BACKEND",
			"ARIZE_INSTALL_BACKEND": "phoenix",
		}),
		Stdout: &stdout,
		Stderr: io.Discard,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if code != 0 {
		t.Errorf("Dispatch exit code = %d, want 0", code)
	}
	got := strings.TrimSpace(stdout.String())
	if got != "phoenix" {
		t.Errorf("child stdout = %q, want %q", got, "phoenix")
	}
}
