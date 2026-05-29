package main

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/bootstrap"
	axexec "github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/exec"
	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/manifest"
)

// installFlags holds per-command flag values. Pointers distinguish "unset"
// from "explicitly false" so we can pass selectively to the Python wizard.
type installFlags struct {
	backend         string
	spaceID         string
	otlpEndpoint    string
	phoenixEndpoint string
	projectName     string
	userID          string
	logPrompts      *bool
	logToolDetails  *bool
	logToolContent  *bool
	verbose         *bool
	withSkills      bool
	branch          string
	nonInteractive  bool
}

func init() {
	// Load manifest to enumerate harnesses. If load fails, fall back to a
	// hardcoded list so --help still works on a broken install.
	var harnessNames []string
	if m, err := manifest.Load(); err == nil {
		harnessNames = m.HarnessNames()
	} else {
		harnessNames = []string{"claude_code", "codex", "copilot", "cursor", "gemini", "kiro"}
	}

	addCmd := &cobra.Command{
		Use:   "add <harness>",
		Short: "Install and configure tracing for a harness",
		Long:  "Install and configure tracing for a supported coding harness. Pass a harness name as the subcommand (claude, codex, copilot, cursor, gemini, kiro).",
	}

	for _, name := range harnessNames {
		harnessName := name
		userFacingName := strings.TrimSuffix(harnessName, "_code")
		f := &installFlags{}

		cmd := &cobra.Command{
			Use:   userFacingName,
			Short: fmt.Sprintf("Install and configure %s tracing", userFacingName),
			RunE: func(cmd *cobra.Command, args []string) error {
				return runInstall(cmd.Context(), harnessName, f)
			},
		}
		addInstallFlags(cmd, f)
		addCmd.AddCommand(cmd)
	}

	rootCmd.AddCommand(addCmd)
}

func addInstallFlags(cmd *cobra.Command, f *installFlags) {
	cmd.Flags().StringVar(&f.backend, "backend", "", "Backend: arize or phoenix")
	cmd.Flags().StringVar(&f.spaceID, "space-id", "", "Arize space ID (with --backend arize)")
	cmd.Flags().StringVar(&f.otlpEndpoint, "otlp-endpoint", "", "OTLP endpoint (defaults to otlp.arize.com:443)")
	cmd.Flags().StringVar(&f.phoenixEndpoint, "phoenix-endpoint", "", "Phoenix collector endpoint")
	cmd.Flags().StringVar(&f.projectName, "project-name", "", "Project name (defaults to harness name)")
	cmd.Flags().StringVar(&f.userID, "user-id", "", "User ID (defaults to empty)")

	// Tri-state booleans: register pflag-level defaults, then read in PreRunE
	// only when the user explicitly set them. nil means "let Python prompt".
	logPrompts := cmd.Flags().Bool("log-prompts", true, "Log user prompts (default true)")
	logToolDetails := cmd.Flags().Bool("log-tool-details", true, "Log tool inputs (default true)")
	logToolContent := cmd.Flags().Bool("log-tool-content", true, "Log tool outputs (default true)")
	verbose := cmd.Flags().Bool("verbose", false, "Print trace summaries to terminal (default false)")

	cmd.PreRunE = func(cmd *cobra.Command, args []string) error {
		if cmd.Flags().Changed("log-prompts") {
			f.logPrompts = logPrompts
		}
		if cmd.Flags().Changed("log-tool-details") {
			f.logToolDetails = logToolDetails
		}
		if cmd.Flags().Changed("log-tool-content") {
			f.logToolContent = logToolContent
		}
		if cmd.Flags().Changed("verbose") {
			f.verbose = verbose
		}
		return nil
	}

	cmd.Flags().BoolVar(&f.withSkills, "with-skills", false, "Symlink harness skills into .agents/skills/")
	cmd.Flags().StringVar(&f.branch, "branch", "main", "Git ref for the install")
	cmd.Flags().BoolVar(&f.nonInteractive, "non-interactive", false, "Error on missing fields instead of prompting")
}

func runInstall(ctx context.Context, harnessName string, f *installFlags) error {
	if !isTTY(os.Stdin) {
		f.nonInteractive = true
	}

	if _, err := bootstrap.Bootstrap(ctx, bootstrap.Options{Branch: f.branch}); err != nil {
		return fmt.Errorf("bootstrap: %w", err)
	}

	envOpts := axexec.InstallEnv{NonInteractive: f.nonInteractive}
	if f.backend != "" {
		envOpts.Backend = &f.backend
	}
	if f.spaceID != "" {
		envOpts.SpaceID = &f.spaceID
	}
	if f.otlpEndpoint != "" {
		envOpts.OTLPEndpoint = &f.otlpEndpoint
	}
	if f.phoenixEndpoint != "" {
		envOpts.PhoenixEndpoint = &f.phoenixEndpoint
	}
	if f.projectName != "" {
		envOpts.ProjectName = &f.projectName
	}
	if f.userID != "" {
		envOpts.UserID = &f.userID
	}
	envOpts.LogPrompts = f.logPrompts
	envOpts.LogToolDetails = f.logToolDetails
	envOpts.LogToolContent = f.logToolContent
	envOpts.Verbose = f.verbose

	env := axexec.BuildInstallEnv(envOpts)

	var args []string
	if f.withSkills {
		args = append(args, "--with-skills")
	}

	binName := fmt.Sprintf("arize-setup-%s", strings.TrimSuffix(harnessName, "_code"))
	exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
		BinName: binName,
		Args:    args,
		Env:     env,
	})
	if err != nil {
		return err
	}
	if exitCode != 0 {
		os.Exit(exitCode)
	}
	return nil
}

// isTTY returns true if f is a terminal.
func isTTY(f *os.File) bool {
	info, err := f.Stat()
	if err != nil {
		return false
	}
	return (info.Mode() & os.ModeCharDevice) != 0
}
