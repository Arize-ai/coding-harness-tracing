//go:build !windows

package bootstrap

import (
	"errors"
	"os"
	"syscall"
)

// platformTryLock attempts a non-blocking exclusive flock. Returns
// errLockBusy when another process holds it.
func platformTryLock(f *os.File) error {
	err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err == nil {
		return nil
	}
	if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
		return errLockBusy
	}
	return err
}

func platformUnlock(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
}
