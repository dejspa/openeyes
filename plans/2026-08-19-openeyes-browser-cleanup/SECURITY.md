# Security review

Approved after fixes.

- Process identity is pinned with Linux pidfds before validation and signaling.
- Cleanup requires exact executable name, root-process shape, CDP port, and profile path.
- Registry removal waits for confirmed process exit; timeout and mismatch fail closed.
- Session state and lock files reject symlinks/non-owned/non-regular files and use mode 0600.
- Atomic state writes use unpredictable temporary files and propagate failures.
- Dashboard health checks are read-only.

Residual risk: systemd installation is host-specific, and the timer runs while the user systemd manager is active (`Linger=no`).
