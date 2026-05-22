package main

import (
	"context"
	"fmt"
	"os"

	"github.com/spf13/cobra"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/doctor"
)

func init() {
	cmd := &cobra.Command{
		Use:   "doctor",
		Short: "Run health checks against the install",
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDoctor(cmd.Context())
		},
	}
	rootCmd.AddCommand(cmd)
}

func runDoctor(ctx context.Context) error {
	verdicts, err := doctor.Run(ctx, doctor.Options{})
	if err != nil {
		return err
	}

	anyFail := false
	for _, v := range verdicts {
		mark := "[OK]"
		if !v.Pass {
			mark = "[FAIL]"
			anyFail = true
		}
		fmt.Printf("  %s %s — %s\n", mark, v.Name, v.Detail)
		if !v.Pass && v.Remediate != "" {
			fmt.Printf("    -> %s\n", v.Remediate)
		}
	}

	if anyFail {
		os.Exit(3)
	}
	return nil
}
