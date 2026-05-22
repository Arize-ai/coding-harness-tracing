//go:build windows

package bootstrap

import "os"

// platformTryLock is a best-effort no-op on Windows. Concurrent ax-trace runs
// on Windows are documented as user error in v1; a future revision can call
// LockFileEx via syscall.
func platformTryLock(_ *os.File) error {
	return nil
}

func platformUnlock(_ *os.File) error {
	return nil
}
