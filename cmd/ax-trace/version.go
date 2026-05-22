package main

import (
	"fmt"

	"github.com/spf13/cobra"

	versionpkg "github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/version"
)

func init() {
	cmd := &cobra.Command{
		Use:   "version",
		Short: "Print ax-trace + package + Python versions",
		RunE: func(cmd *cobra.Command, args []string) error {
			info := versionpkg.Gather(version, commit)
			fmt.Printf("ax-trace %s (commit %s)\n", info.BinaryVersion, info.BinaryCommit)
			fmt.Printf("coding-harness-tracing %s\n", info.PackageVersion)
			fmt.Printf("python %s\n", info.PythonVersion)
			fmt.Printf("venv: %s\n", info.VenvPath)
			return nil
		},
	}
	rootCmd.AddCommand(cmd)
}
