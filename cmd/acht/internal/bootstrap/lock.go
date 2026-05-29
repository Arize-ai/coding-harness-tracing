package bootstrap

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/Arize-ai/coding-harness-tracing/cmd/acht/internal/paths"
)

// lockTimeout is how long AcquireLock will retry before giving up.
const lockTimeout = 60 * time.Second

// lockPollInterval is how frequently AcquireLock retries while waiting.
const lockPollInterval = 250 * time.Millisecond

var (
	lockMu     sync.Mutex
	lockHandle *os.File
)

// AcquireLock takes a flock-style mutex on ~/.arize/acht/bootstrap.lock.
// Times out after 60 seconds with a clear "another acht is bootstrapping"
// message. The mutex is process-scoped — concurrent calls within one process
// serialize via lockMu so callers cannot accidentally double-acquire.
func AcquireLock(ctx context.Context) error {
	if _, err := paths.EnsureAxTraceHome(); err != nil {
		return fmt.Errorf("ensuring acht home: %w", err)
	}
	lockPath, err := paths.LockFile()
	if err != nil {
		return fmt.Errorf("resolving lock file path: %w", err)
	}

	lockMu.Lock()
	if lockHandle != nil {
		lockMu.Unlock()
		return errors.New("AcquireLock: lock already held in this process")
	}
	lockMu.Unlock()

	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return fmt.Errorf("opening lock file %s: %w", lockPath, err)
	}

	deadline := time.Now().Add(lockTimeout)
	for {
		err := platformTryLock(f)
		if err == nil {
			lockMu.Lock()
			lockHandle = f
			lockMu.Unlock()
			return nil
		}
		if !errors.Is(err, errLockBusy) {
			_ = f.Close()
			return fmt.Errorf("acquiring lock %s: %w", lockPath, err)
		}
		if time.Now().After(deadline) {
			_ = f.Close()
			return fmt.Errorf(
				"another acht is bootstrapping (lock %s held for >%s)",
				lockPath, lockTimeout,
			)
		}
		select {
		case <-ctx.Done():
			_ = f.Close()
			return ctx.Err()
		case <-time.After(lockPollInterval):
		}
	}
}

// ReleaseLock releases the bootstrap lock. Safe to call when no lock is held.
func ReleaseLock() error {
	lockMu.Lock()
	defer lockMu.Unlock()
	if lockHandle == nil {
		return nil
	}
	f := lockHandle
	lockHandle = nil
	if err := platformUnlock(f); err != nil {
		_ = f.Close()
		return fmt.Errorf("releasing lock: %w", err)
	}
	return f.Close()
}

// errLockBusy signals that the underlying OS lock is held by another
// process — AcquireLock retries until the timeout elapses.
var errLockBusy = errors.New("lock busy")
