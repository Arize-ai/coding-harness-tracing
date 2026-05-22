// Package exec dispatches into the venv's Python entrypoints.
//
// Used by install/update/uninstall/config commands. Doctor and version do
// NOT go through this package — they're pure Go.
package exec

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/paths"
)

// DispatchOptions controls how a Python entrypoint is invoked.
type DispatchOptions struct {
	BinName string            // e.g. "arize-setup-claude"; resolved to venv/bin/<name> by paths.VenvBin
	Args    []string          // additional CLI args to pass through
	Env     map[string]string // ARIZE_INSTALL_* and other env vars to set on the child
	Stdin   io.Reader         // default os.Stdin
	Stdout  io.Writer         // default os.Stdout
	Stderr  io.Writer         // default os.Stderr
}

// Dispatch resolves the venv binary, sets up the child process, runs it,
// and returns the child's exit code (0 on success, non-zero on failure).
//
// Returns a non-nil error only if the binary couldn't be invoked at all
// (path doesn't exist, permission denied, etc.). A non-zero exit code from
// the child is reflected in the returned int, not the error.
func Dispatch(ctx context.Context, opts DispatchOptions) (int, error) {
	binPath, err := paths.VenvBin(opts.BinName)
	if err != nil {
		return -1, fmt.Errorf("resolving venv binary %q: %w", opts.BinName, err)
	}
	if _, err := os.Stat(binPath); err != nil {
		return -1, fmt.Errorf("venv binary not found at %s: %w", binPath, err)
	}

	cmd := exec.CommandContext(ctx, binPath, opts.Args...)
	if opts.Stdin == nil {
		opts.Stdin = os.Stdin
	}
	if opts.Stdout == nil {
		opts.Stdout = os.Stdout
	}
	if opts.Stderr == nil {
		opts.Stderr = os.Stderr
	}
	cmd.Stdin = opts.Stdin
	cmd.Stdout = opts.Stdout
	cmd.Stderr = opts.Stderr

	cmd.Env = os.Environ()
	for k, v := range opts.Env {
		cmd.Env = append(cmd.Env, fmt.Sprintf("%s=%s", k, v))
	}

	runErr := cmd.Run()
	if runErr == nil {
		return 0, nil
	}
	var exitErr *exec.ExitError
	if errors.As(runErr, &exitErr) {
		return exitErr.ExitCode(), nil
	}
	return -1, runErr
}

// InstallEnv holds the per-flag values used to build the ARIZE_INSTALL_* env
// var map. nil-valued fields are omitted so the Python wizard prompts
// interactively for them.
type InstallEnv struct {
	Backend         *string
	SpaceID         *string
	OTLPEndpoint    *string
	PhoenixEndpoint *string
	ProjectName     *string
	UserID          *string
	LogPrompts      *bool
	LogToolDetails  *bool
	LogToolContent  *bool
	Verbose         *bool
	NonInteractive  bool
}

// BuildInstallEnv constructs the ARIZE_INSTALL_* env-var map from per-flag
// values. nil-valued fields are omitted so the Python wizard prompts
// interactively for them.
func BuildInstallEnv(in InstallEnv) map[string]string {
	out := map[string]string{}
	if in.Backend != nil {
		out["ARIZE_INSTALL_BACKEND"] = *in.Backend
	}
	if in.SpaceID != nil {
		out["ARIZE_INSTALL_SPACE_ID"] = *in.SpaceID
	}
	if in.OTLPEndpoint != nil {
		out["ARIZE_INSTALL_OTLP_ENDPOINT"] = *in.OTLPEndpoint
	}
	if in.PhoenixEndpoint != nil {
		out["ARIZE_INSTALL_PHOENIX_ENDPOINT"] = *in.PhoenixEndpoint
	}
	if in.ProjectName != nil {
		out["ARIZE_INSTALL_PROJECT_NAME"] = *in.ProjectName
	}
	if in.UserID != nil {
		out["ARIZE_INSTALL_USER_ID"] = *in.UserID
	}
	if in.LogPrompts != nil {
		out["ARIZE_INSTALL_LOG_PROMPTS"] = boolStr(*in.LogPrompts)
	}
	if in.LogToolDetails != nil {
		out["ARIZE_INSTALL_LOG_TOOL_DETAILS"] = boolStr(*in.LogToolDetails)
	}
	if in.LogToolContent != nil {
		out["ARIZE_INSTALL_LOG_TOOL_CONTENT"] = boolStr(*in.LogToolContent)
	}
	if in.Verbose != nil {
		out["ARIZE_INSTALL_VERBOSE"] = boolStr(*in.Verbose)
	}
	if in.NonInteractive {
		out["ARIZE_INSTALL_NON_INTERACTIVE"] = "1"
	}
	return out
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
