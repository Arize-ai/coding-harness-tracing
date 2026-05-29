// Package main is the entry point for the acht CLI.
package main

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var (
	version = "dev"
	commit  = "none"
)

var rootCmd = &cobra.Command{
	Use:   "acht",
	Short: "Install and manage Arize coding-harness-tracing",
	Long: `acht is a portable CLI for installing and managing
Arize coding-harness-tracing across Claude Code, Codex, Copilot,
Cursor, Gemini, and Kiro.`,
	SilenceUsage: true,
}

func main() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
