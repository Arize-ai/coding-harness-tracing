package main

import (
	"context"
	"os"

	"github.com/spf13/cobra"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/bootstrap"
	axexec "github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/exec"
)

func init() {
	cmd := &cobra.Command{
		Use:                "config [args...]",
		Short:              "Passthrough to arize-config",
		DisableFlagParsing: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runConfig(cmd.Context(), args)
		},
	}
	rootCmd.AddCommand(cmd)
}

func runConfig(ctx context.Context, args []string) error {
	if _, err := bootstrap.Bootstrap(ctx, bootstrap.Options{Branch: "main"}); err != nil {
		return err
	}
	exitCode, err := axexec.Dispatch(ctx, axexec.DispatchOptions{
		BinName: "arize-config",
		Args:    args,
	})
	if err != nil {
		return err
	}
	if exitCode != 0 {
		os.Exit(exitCode)
	}
	return nil
}
