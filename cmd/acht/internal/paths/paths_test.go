package paths

import (
	"os"
	"path/filepath"
	"runtime"
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

func TestArizeHome(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := ArizeHome()
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(tmp, ".arize")
	if got != want {
		t.Errorf("ArizeHome() = %q, want %q", got, want)
	}
}

func TestInstallDir(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := InstallDir()
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(tmp, ".arize", "harness")
	if got != want {
		t.Errorf("InstallDir() = %q, want %q", got, want)
	}
}

func TestVenvDir(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := VenvDir()
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(tmp, ".arize", "harness", "venv")
	if got != want {
		t.Errorf("VenvDir() = %q, want %q", got, want)
	}
}

func TestVenvPython(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := VenvPython()
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		if !strings.HasSuffix(got, filepath.Join("Scripts", "python.exe")) {
			t.Errorf("VenvPython() = %q, want suffix Scripts/python.exe", got)
		}
	} else {
		if !strings.HasSuffix(got, filepath.Join("bin", "python")) {
			t.Errorf("VenvPython() = %q, want suffix bin/python", got)
		}
	}
}

func TestVenvBin(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := VenvBin("arize-setup-claude")
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		if !strings.HasSuffix(got, filepath.Join("Scripts", "arize-setup-claude.exe")) {
			t.Errorf("VenvBin = %q, want Scripts/arize-setup-claude.exe suffix", got)
		}
	} else {
		if !strings.HasSuffix(got, filepath.Join("bin", "arize-setup-claude")) {
			t.Errorf("VenvBin = %q, want bin/arize-setup-claude suffix", got)
		}
	}
}

func TestAxTraceHome(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := AxTraceHome()
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(tmp, ".arize", "acht")
	if got != want {
		t.Errorf("AxTraceHome() = %q, want %q", got, want)
	}
}

func TestAxTraceSubpaths(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	base := filepath.Join(tmp, ".arize", "acht")
	cases := []struct {
		name string
		fn   func() (string, error)
		want string
	}{
		{"StateFile", StateFile, filepath.Join(base, "state.json")},
		{"LockFile", LockFile, filepath.Join(base, "bootstrap.lock")},
		{"LogFile", LogFile, filepath.Join(base, "bootstrap.log")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := tc.fn()
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.want {
				t.Errorf("%s = %q, want %q", tc.name, got, tc.want)
			}
		})
	}
}

func TestConfigFile(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	got, err := ConfigFile()
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(tmp, ".arize", "harness", "config.yaml")
	if got != want {
		t.Errorf("ConfigFile() = %q, want %q", got, want)
	}
}

func TestEnsureAxTraceHome_Creates(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	dir, err := EnsureAxTraceHome()
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("EnsureAxTraceHome did not create %s: %v", dir, err)
	}
	if !info.IsDir() {
		t.Errorf("EnsureAxTraceHome path %s is not a directory", dir)
	}
	if _, err := EnsureAxTraceHome(); err != nil {
		t.Errorf("second call failed: %v", err)
	}
}
