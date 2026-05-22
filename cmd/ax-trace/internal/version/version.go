// Package version reports ax-trace's own version plus the versions of its
// Python dependencies.
package version

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/paths"
)

const (
	notInstalled = "(not installed)"
	noVenv       = "(no venv)"
)

// Info bundles all version data.
type Info struct {
	BinaryVersion  string // from ldflags
	BinaryCommit   string // from ldflags
	PackageVersion string // from venv dist-info; "(not installed)" if absent
	PythonVersion  string // from venv python --version; "(no venv)" if absent
	VenvPath       string // resolved venv path
}

// Gather collects version info from disk.
//
// binVersion and binCommit come from main's ldflags and are passed in here
// rather than read from build info, because callers want to control which
// values get reported.
func Gather(binVersion, binCommit string) Info {
	info := Info{
		BinaryVersion:  binVersion,
		BinaryCommit:   binCommit,
		PackageVersion: notInstalled,
		PythonVersion:  noVenv,
	}

	if venv, err := paths.VenvDir(); err == nil {
		info.VenvPath = venv
	}

	if pkg, err := FindPackageVersion(); err == nil {
		info.PackageVersion = pkg
	}

	if py, err := FindPythonVersion(); err == nil {
		info.PythonVersion = py
	}

	return info
}

// FindPackageVersion reads the venv's site-packages directory and returns the
// Version: line from the coding_harness_tracing-*.dist-info/METADATA file.
func FindPackageVersion() (string, error) {
	siteRoots, err := sitePackagesCandidates()
	if err != nil {
		return "", err
	}

	for _, root := range siteRoots {
		matches, err := filepath.Glob(filepath.Join(root, "coding_harness_tracing-*.dist-info"))
		if err != nil {
			return "", fmt.Errorf("globbing dist-info under %s: %w", root, err)
		}
		for _, dir := range matches {
			metadata := filepath.Join(dir, "METADATA")
			v, err := readMetadataVersion(metadata)
			if err == nil {
				return v, nil
			}
		}
	}

	return "", fmt.Errorf("no coding_harness_tracing dist-info found")
}

// FindPythonVersion exec's the venv's python binary with --version and returns
// the output trimmed of whitespace.
func FindPythonVersion() (string, error) {
	py, err := paths.VenvPython()
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(py); err != nil {
		return "", fmt.Errorf("venv python not found at %s: %w", py, err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	out, err := exec.CommandContext(ctx, py, "--version").Output()
	if err != nil {
		return "", fmt.Errorf("running %s --version: %w", py, err)
	}
	return strings.TrimSpace(string(out)), nil
}

// sitePackagesCandidates returns the platform-appropriate site-packages
// directories that may contain dist-info entries.
func sitePackagesCandidates() ([]string, error) {
	venv, err := paths.VenvDir()
	if err != nil {
		return nil, err
	}

	if runtime.GOOS == "windows" {
		return []string{filepath.Join(venv, "Lib", "site-packages")}, nil
	}

	// On Unix, site-packages lives under lib/pythonX.Y/site-packages.
	matches, err := filepath.Glob(filepath.Join(venv, "lib", "python*", "site-packages"))
	if err != nil {
		return nil, fmt.Errorf("globbing site-packages under %s: %w", venv, err)
	}
	return matches, nil
}

// readMetadataVersion scans a PEP 566 METADATA file for the Version: header
// and returns its value.
func readMetadataVersion(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		// METADATA headers end at the first blank line.
		if line == "" {
			break
		}
		if v, ok := strings.CutPrefix(line, "Version:"); ok {
			return strings.TrimSpace(v), nil
		}
	}
	if err := scanner.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("no Version: header in %s", path)
}
