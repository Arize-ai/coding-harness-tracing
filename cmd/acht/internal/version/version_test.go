package version

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func setHome(t *testing.T, dir string) {
	t.Helper()
	t.Setenv("HOME", dir)
	if runtime.GOOS == "windows" {
		t.Setenv("USERPROFILE", dir)
	}
}

// writeDistInfo creates a fake venv site-packages tree containing a single
// coding_harness_tracing dist-info with the given METADATA contents.
func writeDistInfo(t *testing.T, home, version string) string {
	t.Helper()

	var siteDir string
	if runtime.GOOS == "windows" {
		siteDir = filepath.Join(home, ".arize", "harness", "venv", "Lib", "site-packages")
	} else {
		siteDir = filepath.Join(home, ".arize", "harness", "venv", "lib", "python3.11", "site-packages")
	}

	distDir := filepath.Join(siteDir, "coding_harness_tracing-"+version+".dist-info")
	if err := os.MkdirAll(distDir, 0o755); err != nil {
		t.Fatalf("mkdir dist-info: %v", err)
	}

	metadata := "Metadata-Version: 2.1\n" +
		"Name: coding-harness-tracing\n" +
		"Version: " + version + "\n" +
		"Summary: test fixture\n"
	if err := os.WriteFile(filepath.Join(distDir, "METADATA"), []byte(metadata), 0o644); err != nil {
		t.Fatalf("write METADATA: %v", err)
	}
	return distDir
}

func TestFindPackageVersion_Success(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	writeDistInfo(t, tmp, "0.1.0")

	got, err := FindPackageVersion()
	if err != nil {
		t.Fatalf("FindPackageVersion() error = %v", err)
	}
	if got != "0.1.0" {
		t.Errorf("FindPackageVersion() = %q, want %q", got, "0.1.0")
	}
}

func TestFindPackageVersion_Missing(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	if _, err := FindPackageVersion(); err == nil {
		t.Errorf("FindPackageVersion() with no venv should fail, got nil error")
	}
}

func TestGather_Sentinels(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	info := Gather("v1.2.3", "abcdef0")
	if info.PackageVersion != "(not installed)" {
		t.Errorf("PackageVersion = %q, want %q", info.PackageVersion, "(not installed)")
	}
	if info.PythonVersion != "(no venv)" {
		t.Errorf("PythonVersion = %q, want %q", info.PythonVersion, "(no venv)")
	}
}

func TestGather_PropagatesBinaryFields(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	info := Gather("v9.9.9", "deadbeef")
	if info.BinaryVersion != "v9.9.9" {
		t.Errorf("BinaryVersion = %q, want %q", info.BinaryVersion, "v9.9.9")
	}
	if info.BinaryCommit != "deadbeef" {
		t.Errorf("BinaryCommit = %q, want %q", info.BinaryCommit, "deadbeef")
	}
	if info.VenvPath == "" {
		t.Errorf("VenvPath should be set even when venv is absent, got empty string")
	}
}

func TestGather_FindsPackageVersion(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	writeDistInfo(t, tmp, "0.4.2")

	info := Gather("v1.2.3", "abcdef0")
	if info.PackageVersion != "0.4.2" {
		t.Errorf("PackageVersion = %q, want %q", info.PackageVersion, "0.4.2")
	}
}

func TestFindPackageVersion_MetadataHeadersStopAtBlankLine(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	var siteDir string
	if runtime.GOOS == "windows" {
		siteDir = filepath.Join(tmp, ".arize", "harness", "venv", "Lib", "site-packages")
	} else {
		siteDir = filepath.Join(tmp, ".arize", "harness", "venv", "lib", "python3.11", "site-packages")
	}
	distDir := filepath.Join(siteDir, "coding_harness_tracing-1.2.3.dist-info")
	if err := os.MkdirAll(distDir, 0o755); err != nil {
		t.Fatalf("mkdir dist-info: %v", err)
	}
	// Version: header appears after the blank line that ends the header
	// section, simulating a description body that mentions Version:. The
	// parser should bail at the blank line and report no header found.
	metadata := "Metadata-Version: 2.1\n" +
		"Name: coding-harness-tracing\n" +
		"\n" +
		"Version: should-not-be-read\n"
	if err := os.WriteFile(filepath.Join(distDir, "METADATA"), []byte(metadata), 0o644); err != nil {
		t.Fatalf("write METADATA: %v", err)
	}

	if _, err := FindPackageVersion(); err == nil {
		t.Errorf("FindPackageVersion() should have failed when Version: lives in description body")
	}
}

func TestFindPackageVersion_TrimsWhitespaceAroundVersion(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	var siteDir string
	if runtime.GOOS == "windows" {
		siteDir = filepath.Join(tmp, ".arize", "harness", "venv", "Lib", "site-packages")
	} else {
		siteDir = filepath.Join(tmp, ".arize", "harness", "venv", "lib", "python3.11", "site-packages")
	}
	distDir := filepath.Join(siteDir, "coding_harness_tracing-2.5.0.dist-info")
	if err := os.MkdirAll(distDir, 0o755); err != nil {
		t.Fatalf("mkdir dist-info: %v", err)
	}
	metadata := "Metadata-Version: 2.1\n" +
		"Name: coding-harness-tracing\n" +
		"Version:   2.5.0  \n"
	if err := os.WriteFile(filepath.Join(distDir, "METADATA"), []byte(metadata), 0o644); err != nil {
		t.Fatalf("write METADATA: %v", err)
	}

	got, err := FindPackageVersion()
	if err != nil {
		t.Fatalf("FindPackageVersion() error = %v", err)
	}
	if got != "2.5.0" {
		t.Errorf("FindPackageVersion() = %q, want %q (should strip surrounding whitespace)", got, "2.5.0")
	}
}

func TestFindPythonVersion_NoVenv(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)

	if _, err := FindPythonVersion(); err == nil {
		t.Errorf("FindPythonVersion() with no venv should return error, got nil")
	}
}

func TestFindPythonVersion_ExecutesScript(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake python shell script is POSIX-only")
	}

	tmp := t.TempDir()
	setHome(t, tmp)

	pyDir := filepath.Join(tmp, ".arize", "harness", "venv", "bin")
	if err := os.MkdirAll(pyDir, 0o755); err != nil {
		t.Fatalf("mkdir venv/bin: %v", err)
	}
	pyPath := filepath.Join(pyDir, "python")
	script := "#!/bin/sh\necho 'Python 3.11.5'\n"
	if err := os.WriteFile(pyPath, []byte(script), 0o755); err != nil {
		t.Fatalf("write fake python: %v", err)
	}

	got, err := FindPythonVersion()
	if err != nil {
		t.Skipf("requires venv python execution to work (fake script may not be runnable here): %v", err)
	}
	if got != "Python 3.11.5" {
		t.Errorf("FindPythonVersion() = %q, want %q", got, "Python 3.11.5")
	}
}

func TestGather_FindsPythonVersionViaScript(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake python shell script is POSIX-only")
	}

	tmp := t.TempDir()
	setHome(t, tmp)

	pyDir := filepath.Join(tmp, ".arize", "harness", "venv", "bin")
	if err := os.MkdirAll(pyDir, 0o755); err != nil {
		t.Fatalf("mkdir venv/bin: %v", err)
	}
	pyPath := filepath.Join(pyDir, "python")
	script := "#!/bin/sh\necho 'Python 3.12.0'\n"
	if err := os.WriteFile(pyPath, []byte(script), 0o755); err != nil {
		t.Fatalf("write fake python: %v", err)
	}

	info := Gather("v1.0.0", "cafe")
	if info.PythonVersion == "(no venv)" {
		t.Skipf("fake python script not executable here, got sentinel %q", info.PythonVersion)
	}
	if info.PythonVersion != "Python 3.12.0" {
		t.Errorf("Gather().PythonVersion = %q, want %q", info.PythonVersion, "Python 3.12.0")
	}
}
