// Package doctor runs health checks against the user's install.
//
// Checks per harness:
//   - settings file exists at the path from manifest and (for JSON files) is parseable
//   - relevant env vars are set OR the value is in ~/.arize/harness/config.yaml
//
// Plus shared checks:
//   - venv python interpreter exists on disk
//   - OTLP endpoint returns HTTP < 500 within 5 seconds
package doctor

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace/internal/manifest"
)

// Verdict represents one check result.
type Verdict struct {
	Name      string
	Pass      bool
	Detail    string // human-readable explanation
	Remediate string // optional suggested fix
}

// Options configures the doctor run. Each field is optional; zero values pick
// sensible defaults (real $HOME, real http.DefaultClient, real time.Now).
type Options struct {
	HTTPClient *http.Client
	NowFunc    func() time.Time
	// HomeDir overrides $HOME for filesystem lookups. When empty, the real
	// user home directory is used.
	HomeDir string
	// OTLPEndpoint overrides the manifest-default OTLP endpoint. When empty,
	// the manifest's shared.otlp_endpoint_default is used.
	OTLPEndpoint string
}

// Run executes all checks and returns the list of verdicts.
func Run(ctx context.Context, opts Options) ([]Verdict, error) {
	m, err := manifest.Load()
	if err != nil {
		return nil, fmt.Errorf("loading manifest: %w", err)
	}
	verdicts := []Verdict{}
	verdicts = append(verdicts, CheckVenv(opts))
	for _, name := range m.HarnessNames() {
		entry := m.Harnesses[name]
		verdicts = append(verdicts, CheckHarnessSettings(name, entry, opts))
		verdicts = append(verdicts, CheckHarnessEnv(name, entry, opts))
	}
	endpoint := opts.OTLPEndpoint
	if endpoint == "" {
		endpoint = m.Shared.OtlpEndpointDefault
	}
	verdicts = append(verdicts, CheckOTLPEndpoint(ctx, endpoint, opts))
	return verdicts, nil
}
