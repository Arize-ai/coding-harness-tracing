//go:build !windows

package bootstrap

import (
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// TestAcquireLock_TimesOutWhenHeld pre-acquires the OS-level flock from a
// separate file descriptor, then expects AcquireLock to poll and time out
// rather than blocking indefinitely or returning instantly.
func TestAcquireLock_TimesOutWhenHeld(t *testing.T) {
	tmp := t.TempDir()
	setHome(t, tmp)
	t.Cleanup(func() { _ = ReleaseLock() })

	lockPath := filepath.Join(tmp, ".arize", "acht", "bootstrap.lock")
	if err := os.MkdirAll(filepath.Dir(lockPath), 0o755); err != nil {
		t.Fatal(err)
	}
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("unable to pre-acquire flock: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 750*time.Millisecond)
	defer cancel()
	start := time.Now()
	err = AcquireLock(ctx)
	elapsed := time.Since(start)
	if err == nil {
		_ = ReleaseLock()
		t.Fatal("expected AcquireLock to fail while another holder owns the lock")
	}
	if elapsed < 500*time.Millisecond {
		t.Errorf("AcquireLock returned too fast (%s) — expected polling", elapsed)
	}
}
