package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/bootstrap"
	axexec "github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/exec"
	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/paths"
)

var updateBranch string

func init() {
	cmd := &cobra.Command{
		Use:   "update",
		Short: "Update coding-harness-tracing and re-register installed harnesses",
		RunE: func(cmd *cobra.Command, args []string) error {
			return runUpdate(cmd.Context())
		},
	}
	cmd.Flags().StringVar(&updateBranch, "branch", "main", "Git ref for the update")
	rootCmd.AddCommand(cmd)
}

func runUpdate(ctx context.Context) error {
	result, err := bootstrap.Bootstrap(ctx, bootstrap.Options{Branch: updateBranch})
	if err != nil {
		return fmt.Errorf("bootstrap: %w", err)
	}

	installDir, err := paths.InstallDir()
	if err != nil {
		return fmt.Errorf("resolving install dir: %w", err)
	}

	// Bootstrap reuses a healthy venv (see internal/bootstrap/venv.go), so it
	// only runs `uv pip install` on first creation. After EnsureRepo pulls the
	// updated source, the venv's installed copy of `core` would still be the
	// old one — list_installed_harnesses, the install.py wizards, and the
	// prompt helpers would all run stale code. Refresh the package explicitly
	// to mirror install.sh's `update` branch (install.sh:308-327).
	if err := refreshVenvPackage(ctx, result.UvPath, result.VenvPython, installDir); err != nil {
		return fmt.Errorf("refreshing venv package: %w", err)
	}

	harnesses, err := listInstalledHarnesses(ctx)
	if err != nil {
		return fmt.Errorf("listing installed harnesses: %w", err)
	}
	if len(harnesses) == 0 {
		fmt.Fprintln(os.Stdout, "[acht] no installed harnesses found to re-register")
		fmt.Fprintln(os.Stdout, "[acht] update complete.")
		return nil
	}

	for _, key := range harnesses {
		installPy := filepath.Join(installDir, "tracing", harnessSubdir(key), "install.py")
		if _, statErr := os.Stat(installPy); statErr != nil {
			fmt.Fprintf(os.Stderr, "[acht] harness install script not found for %q (skipping): %v\n", key, statErr)
			continue
		}
		fmt.Fprintf(os.Stdout, "[acht] re-registering %s...\n", key)
		exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
			BinName: "python",
			Args:    []string{installPy, "install"},
		})
		if err != nil {
			return fmt.Errorf("re-registering %s: %w", key, err)
		}
		if exitCode != 0 {
			os.Exit(exitCode)
		}
	}

	fmt.Fprintln(os.Stdout, "[acht] update complete.")
	return nil
}

// refreshVenvPackage runs `uv pip install --python <venvPython> -U <installDir>`
// to upgrade the coding-harness-tracing package inside the venv. Necessary
// because bootstrap.Bootstrap short-circuits when the venv is already healthy
// and does not re-install on its own.
func refreshVenvPackage(ctx context.Context, uvPath, venvPython, installDir string) error {
	fmt.Fprintln(os.Stdout, "[acht] reinstalling coding-harness-tracing in venv...")
	cmd := exec.CommandContext(ctx, uvPath, "pip", "install", "--python", venvPython, "-U", installDir)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return fmt.Errorf("uv pip install exited with code %d", exitErr.ExitCode())
		}
		return fmt.Errorf("uv pip install: %w", err)
	}
	return nil
}

// listInstalledHarnesses calls core.setup.list_installed_harnesses via the venv
// python and returns the harness keys from config.yaml.
func listInstalledHarnesses(ctx context.Context) ([]string, error) {
	var buf bytes.Buffer
	exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
		BinName: "python",
		Args: []string{
			"-c",
			"from core.setup import list_installed_harnesses as L\nprint(\"\\n\".join(L()))",
		},
		Stdout: &buf,
		Stderr: os.Stderr,
	})
	if err != nil {
		return nil, err
	}
	if exitCode != 0 {
		return nil, fmt.Errorf("list_installed_harnesses exited with code %d", exitCode)
	}
	return parseHarnessList(buf.String()), nil
}

// parseHarnessList splits the newline-delimited output from
// list_installed_harnesses into a slice, skipping blank lines.
func parseHarnessList(out string) []string {
	var keys []string
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		keys = append(keys, line)
	}
	return keys
}

// harnessSubdir maps a harness key (as stored in config.yaml) to its
// tracing/<subdir> directory name. Mirrors core.setup.harness_dir: replace
// dashes with underscores (e.g. "claude-code" -> "claude_code").
func harnessSubdir(key string) string {
	return strings.ReplaceAll(key, "-", "_")
}
