package bootstrap

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/paths"
)

// repoTarballURLFormat is the GitHub tarball URL template for a branch.
// %s is replaced with the branch name. var (not const) so tests in this
// package can swap in an httptest URL; not part of the public API.
var repoTarballURLFormat = "https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/%s.tar.gz"

// EnsureRepo synchronizes ~/.arize/harness with origin/<branch>.
// If .git exists, git fetch + checkout. Otherwise download the tarball.
func EnsureRepo(ctx context.Context, opts Options) error {
	opts = withDefaults(opts)

	installDir, err := paths.InstallDir()
	if err != nil {
		return fmt.Errorf("resolving install dir: %w", err)
	}

	gitDir := filepath.Join(installDir, ".git")
	if info, err := os.Stat(gitDir); err == nil && info.IsDir() {
		return gitSyncRepo(ctx, opts, installDir, opts.Branch)
	}

	return downloadAndExtractTarball(ctx, opts, installDir, opts.Branch)
}

// gitSyncRepo runs `git fetch + checkout` to update the existing clone.
// Mirrors install.sh's git_sync_harness_repo: prefer shallow fetch, fall back
// to full fetch, then pull --ff-only as a last resort.
func gitSyncRepo(ctx context.Context, opts Options, installDir, branch string) error {
	fmt.Fprintf(opts.Stdout, "[acht] syncing %s with origin/%s\n", installDir, branch)

	attempts := [][]string{
		{"-C", installDir, "fetch", "--depth", "1", "origin", branch},
		{"-C", installDir, "fetch", "origin", branch},
	}
	var fetchErr error
	for _, args := range attempts {
		fetchErr = opts.Runner.Run(ctx, "git", args, nil, opts.Stdout, opts.Stderr, nil)
		if fetchErr == nil {
			break
		}
	}
	if fetchErr != nil {
		return fmt.Errorf("git fetch failed: %w", fetchErr)
	}

	if err := opts.Runner.Run(ctx, "git",
		[]string{"-C", installDir, "checkout", "-B", branch, "FETCH_HEAD"},
		nil, opts.Stdout, opts.Stderr, nil); err != nil {
		return fmt.Errorf("git checkout failed: %w", err)
	}
	return nil
}

// downloadAndExtractTarball downloads the GitHub tarball for branch and
// extracts it into installDir, stripping the top-level directory.
func downloadAndExtractTarball(ctx context.Context, opts Options, installDir, branch string) error {
	url := fmt.Sprintf(repoTarballURLFormat, branch)
	fmt.Fprintf(opts.Stdout, "[acht] downloading repo tarball from %s\n", url)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("building tarball request: %w", err)
	}
	resp, err := opts.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("downloading tarball: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("downloading tarball: HTTP %d from %s", resp.StatusCode, url)
	}

	if err := os.MkdirAll(installDir, 0o755); err != nil {
		return fmt.Errorf("creating install dir %s: %w", installDir, err)
	}
	return extractTarGz(resp.Body, installDir)
}

// extractTarGz extracts a gzipped tar stream into destDir, stripping the
// first path component (GitHub tarballs wrap everything in a top-level dir).
func extractTarGz(r io.Reader, destDir string) error {
	gz, err := gzip.NewReader(r)
	if err != nil {
		return fmt.Errorf("opening gzip stream: %w", err)
	}
	defer func() { _ = gz.Close() }()

	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("reading tar entry: %w", err)
		}

		rel := stripFirstComponent(hdr.Name)
		if rel == "" {
			continue
		}
		if !isSafeRelPath(rel) {
			return fmt.Errorf("refusing tar entry with unsafe path: %s", hdr.Name)
		}
		target := filepath.Join(destDir, rel)

		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return fmt.Errorf("creating dir %s: %w", target, err)
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return fmt.Errorf("creating parent dir for %s: %w", target, err)
			}
			mode := os.FileMode(hdr.Mode).Perm()
			if mode == 0 {
				mode = 0o644
			}
			f, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
			if err != nil {
				return fmt.Errorf("creating file %s: %w", target, err)
			}
			if _, err := io.Copy(f, tr); err != nil {
				_ = f.Close()
				return fmt.Errorf("writing %s: %w", target, err)
			}
			if err := f.Close(); err != nil {
				return fmt.Errorf("closing %s: %w", target, err)
			}
		case tar.TypeSymlink:
			if !isSafeSymlinkTarget(target, hdr.Linkname, destDir) {
				return fmt.Errorf("refusing tar symlink with unsafe target: %s -> %s", hdr.Name, hdr.Linkname)
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return fmt.Errorf("creating parent dir for %s: %w", target, err)
			}
			_ = os.Remove(target)
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return fmt.Errorf("creating symlink %s -> %s: %w", target, hdr.Linkname, err)
			}
		default:
			// Skip other entry types (block devices, fifos, etc.) — never
			// present in our repo tarball.
		}
	}
}

// stripFirstComponent removes the leading path component, matching tar
// --strip-components=1. Returns "" if nothing remains.
func stripFirstComponent(name string) string {
	clean := strings.TrimPrefix(filepath.ToSlash(name), "./")
	idx := strings.IndexByte(clean, '/')
	if idx < 0 {
		return ""
	}
	rest := clean[idx+1:]
	rest = strings.TrimPrefix(rest, "/")
	return rest
}

// isSafeRelPath rejects entries that try to escape destDir via .. or
// absolute paths.
func isSafeRelPath(rel string) bool {
	if rel == "" {
		return false
	}
	if filepath.IsAbs(rel) {
		return false
	}
	for _, part := range strings.Split(filepath.ToSlash(rel), "/") {
		if part == ".." {
			return false
		}
	}
	return true
}

// isSafeSymlinkTarget returns true when following the symlink at linkPath
// pointing to linkname would resolve to a path inside destDir. Rejects
// absolute targets outright, but allows relative targets that traverse up
// and back down as long as the resolved path stays under destDir
// (e.g. tracing/claude_code/core -> ../../core resolves to <destDir>/core).
// Both linkPath and destDir should be absolute cleaned paths.
func isSafeSymlinkTarget(linkPath, linkname, destDir string) bool {
	if linkname == "" {
		return false
	}
	if filepath.IsAbs(linkname) {
		return false
	}
	resolved := filepath.Clean(filepath.Join(filepath.Dir(linkPath), linkname))
	rel, err := filepath.Rel(destDir, resolved)
	if err != nil {
		return false
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	return true
}
