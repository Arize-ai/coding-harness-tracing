// Package paths resolves cross-platform locations used by acht.
//
// Two roots:
//   - ArizeHome:   ~/.arize         — shared with install.sh; contains harness/
//   - AxTraceHome: ~/.arize/acht — Go-owned; uv-path cache, lock, logs
package paths

import (
	"os"
	"path/filepath"
	"runtime"
)

// Home returns the user's home directory. Errors are fatal — callers can
// trust the path is usable.
func Home() (string, error) {
	return os.UserHomeDir()
}

// ArizeHome returns ~/.arize.
func ArizeHome() (string, error) {
	home, err := Home()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".arize"), nil
}

// InstallDir returns ~/.arize/harness — the shared install directory used by both
// install.sh and acht.
func InstallDir() (string, error) {
	root, err := ArizeHome()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "harness"), nil
}

// VenvDir returns ~/.arize/harness/venv.
func VenvDir() (string, error) {
	install, err := InstallDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(install, "venv"), nil
}

// VenvPython returns the path to the venv's Python interpreter.
// On Unix: <venv>/bin/python. On Windows: <venv>\Scripts\python.exe.
func VenvPython() (string, error) {
	venv, err := VenvDir()
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "windows" {
		return filepath.Join(venv, "Scripts", "python.exe"), nil
	}
	return filepath.Join(venv, "bin", "python"), nil
}

// VenvBin returns the path to a named executable inside the venv.
// On Unix: <venv>/bin/<name>. On Windows: <venv>\Scripts\<name>.exe.
func VenvBin(name string) (string, error) {
	venv, err := VenvDir()
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "windows" {
		return filepath.Join(venv, "Scripts", name+".exe"), nil
	}
	return filepath.Join(venv, "bin", name), nil
}

// AxTraceHome returns ~/.arize/acht.
func AxTraceHome() (string, error) {
	root, err := ArizeHome()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "acht"), nil
}

// StateFile returns ~/.arize/acht/state.json — the cache file for the uv
// path and last-known package version.
func StateFile() (string, error) {
	root, err := AxTraceHome()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "state.json"), nil
}

// LockFile returns ~/.arize/acht/bootstrap.lock — the flock-style mutex
// preventing concurrent bootstraps.
func LockFile() (string, error) {
	root, err := AxTraceHome()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "bootstrap.lock"), nil
}

// LogFile returns ~/.arize/acht/bootstrap.log — the log file for failed
// bootstrap runs.
func LogFile() (string, error) {
	root, err := AxTraceHome()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "bootstrap.log"), nil
}

// ConfigFile returns ~/.arize/harness/config.yaml.
func ConfigFile() (string, error) {
	install, err := InstallDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(install, "config.yaml"), nil
}

// EnsureAxTraceHome creates ~/.arize/acht if it doesn't exist.
// Idempotent. Returns the directory path.
func EnsureAxTraceHome() (string, error) {
	dir, err := AxTraceHome()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return dir, nil
}
