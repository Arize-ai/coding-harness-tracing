package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	axexec "github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/exec"
	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/paths"
)

// uninstallSelections holds the per-harness --<harness> boolean flags. When
// none are set, `acht uninstall` does a full wipe. When one or more are
// set, only those harnesses are uninstalled and the shared runtime is left
// in place.
type uninstallSelections struct {
	claude  bool
	codex   bool
	copilot bool
	cursor  bool
	gemini  bool
	kiro    bool
}

// selected returns the harness keys (as they appear in config.yaml) that
// the user opted into via --<harness> flags, in a deterministic order.
func (s *uninstallSelections) selected() []string {
	var keys []string
	if s.claude {
		keys = append(keys, "claude-code")
	}
	if s.codex {
		keys = append(keys, "codex")
	}
	if s.copilot {
		keys = append(keys, "copilot")
	}
	if s.cursor {
		keys = append(keys, "cursor")
	}
	if s.gemini {
		keys = append(keys, "gemini")
	}
	if s.kiro {
		keys = append(keys, "kiro")
	}
	return keys
}

func init() {
	s := &uninstallSelections{}
	cmd := &cobra.Command{
		Use:   "uninstall",
		Short: "Uninstall selected harnesses, or all harnesses + the shared runtime",
		Long: `Uninstall coding-harness-tracing.

With no flags, uninstalls every installed harness and wipes the shared
Python runtime plus acht's own state directory.

With one or more --<harness> flags, uninstalls only the selected harnesses
and leaves the shared runtime in place.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			keys := s.selected()
			if len(keys) == 0 {
				return runUninstallAll(cmd.Context())
			}
			return runUninstallSelected(cmd.Context(), keys)
		},
	}
	cmd.Flags().BoolVar(&s.claude, "claude", false, "Uninstall Claude Code tracing")
	cmd.Flags().BoolVar(&s.codex, "codex", false, "Uninstall Codex CLI tracing")
	cmd.Flags().BoolVar(&s.copilot, "copilot", false, "Uninstall GitHub Copilot tracing")
	cmd.Flags().BoolVar(&s.cursor, "cursor", false, "Uninstall Cursor tracing")
	cmd.Flags().BoolVar(&s.gemini, "gemini", false, "Uninstall Gemini CLI tracing")
	cmd.Flags().BoolVar(&s.kiro, "kiro", false, "Uninstall Kiro tracing")
	rootCmd.AddCommand(cmd)
}

// runUninstallSelected tears down each requested harness, continuing past
// per-harness failures so one broken install.py doesn't block the others.
// Leaves the shared runtime (venv, install dir, acht state) in place.
//
// If the venv is missing, returns nil with a friendly note — there's
// nothing to tear down at the harness level.
func runUninstallSelected(ctx context.Context, keys []string) error {
	if !venvExists() {
		fmt.Fprintln(os.Stdout, "[acht] venv not found — nothing to uninstall")
		return nil
	}

	installDir, err := paths.InstallDir()
	if err != nil {
		return fmt.Errorf("resolving install dir: %w", err)
	}

	var failed []string
	for _, key := range keys {
		installPy := filepath.Join(installDir, "tracing", harnessSubdir(key), "install.py")
		if _, statErr := os.Stat(installPy); statErr != nil {
			fmt.Fprintf(os.Stderr, "[acht] %s install script not found at %s (skipping)\n", key, installPy)
			failed = append(failed, key)
			continue
		}
		fmt.Fprintf(os.Stdout, "[acht] uninstalling %s tracing...\n", key)
		exitCode, dispatchErr := axexec.Dispatch(ctx, axexec.DispatchOptions{
			BinName: "python",
			Args:    []string{installPy, "uninstall"},
		})
		if dispatchErr != nil {
			fmt.Fprintf(os.Stderr, "[acht] %s uninstall failed (continuing): %v\n", key, dispatchErr)
			failed = append(failed, key)
			continue
		}
		if exitCode != 0 {
			fmt.Fprintf(os.Stderr, "[acht] %s uninstall exited with code %d (continuing)\n", key, exitCode)
			failed = append(failed, key)
		}
	}

	if len(failed) > 0 {
		return fmt.Errorf("one or more uninstalls failed: %v", failed)
	}
	return nil
}

// runUninstallAll mirrors install.sh's full-wipe path: tear down each
// installed harness, then run `python -m core.setup.wipe` to remove the
// shared runtime, then delete acht's own state directory.
//
// If the venv is missing, skip directly to deleting the install + acht
// directories.
func runUninstallAll(ctx context.Context) error {
	installDir, err := paths.InstallDir()
	if err != nil {
		return fmt.Errorf("resolving install dir: %w", err)
	}
	axTraceHome, err := paths.AxTraceHome()
	if err != nil {
		return fmt.Errorf("resolving acht home: %w", err)
	}

	if !venvExists() {
		fmt.Fprintln(os.Stdout, "[acht] venv not found — removing install directories")
		if err := os.RemoveAll(installDir); err != nil {
			return fmt.Errorf("removing %s: %w", installDir, err)
		}
		if err := os.RemoveAll(axTraceHome); err != nil {
			return fmt.Errorf("removing %s: %w", axTraceHome, err)
		}
		fmt.Fprintln(os.Stdout, "[acht] uninstall complete.")
		return nil
	}

	harnesses, err := listInstalledHarnesses(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[acht] could not enumerate installed harnesses (continuing): %v\n", err)
	}
	for _, key := range harnesses {
		installPy := filepath.Join(installDir, "tracing", harnessSubdir(key), "install.py")
		if _, statErr := os.Stat(installPy); statErr != nil {
			fmt.Fprintf(os.Stderr, "[acht] harness install script not found for %q (skipping): %v\n", key, statErr)
			continue
		}
		fmt.Fprintf(os.Stdout, "[acht] uninstalling %s tracing...\n", key)
		exitCode, dispatchErr := axexec.Dispatch(ctx, axexec.DispatchOptions{
			BinName: "python",
			Args:    []string{installPy, "uninstall"},
		})
		if dispatchErr != nil {
			fmt.Fprintf(os.Stderr, "[acht] %s uninstall failed (continuing): %v\n", key, dispatchErr)
			continue
		}
		if exitCode != 0 {
			fmt.Fprintf(os.Stderr, "[acht] %s uninstall exited with code %d (continuing)\n", key, exitCode)
		}
	}

	fmt.Fprintln(os.Stdout, "[acht] wiping shared runtime...")
	exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
		BinName: "python",
		Args:    []string{"-m", "core.setup.wipe"},
	})
	if err != nil {
		return fmt.Errorf("wiping shared runtime: %w", err)
	}
	// Match install.sh's `set -e` behavior: if wipe fails (or the user
	// declines its confirmation prompt), abort before deleting acht's own
	// state. This leaves breadcrumbs (bootstrap log, lock file) for debugging
	// a partial uninstall.
	if exitCode != 0 {
		os.Exit(exitCode)
	}

	if err := os.RemoveAll(axTraceHome); err != nil {
		return fmt.Errorf("removing %s: %w", axTraceHome, err)
	}

	fmt.Fprintln(os.Stdout, "[acht] uninstall complete.")
	return nil
}

// venvExists reports whether the venv's python interpreter is present on
// disk. Used to short-circuit uninstall when there's nothing to tear down.
func venvExists() bool {
	pyPath, err := paths.VenvPython()
	if err != nil {
		return false
	}
	_, statErr := os.Stat(pyPath)
	return statErr == nil
}
