package manifest

import _ "embed"

// manifest_embedded.json is a compile-time copy of core/manifest.json.
//
// Keep this file in sync with core/manifest.json. Manual copies are acceptable
// for v1 since the file rarely changes; a future task can add automation
// (e.g. a `go generate` directive or a Makefile step that re-copies before
// `go build`).
//
//go:embed manifest_embedded.json
var embeddedManifest []byte
