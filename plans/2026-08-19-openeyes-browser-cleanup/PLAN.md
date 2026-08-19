---
mode: standard
---

# Plan

- [x] Reproduce the orphan/registry mismatch and document root cause.
- [x] Make dashboard session listing read-only with respect to lifecycle records.
- [x] Make port allocation account for actual live listeners.
- [x] Add verified standalone cleanup for expired tracked and old untracked managed Chromium roots.
- [x] Add regression tests for dashboard, allocation, ownership verification, and cleanup age behavior.
- [x] Add and enable a systemd user timer for 30-minute cleanup.
- [x] Run focused and full test suites.
- [x] Run independent code and security review.
- [x] Record completion evidence.

## Approach

Keep the existing registry and detached-browser contract. Add small process-discovery and ownership-verification helpers in `server.py`, expose cleanup as a console command, and invoke it from a user timer. A browser qualifies as managed only when its root command line has no Chromium `--type=` child marker and has the exact matching `--remote-debugging-port=<port>` and `/tmp/openeyes-web-chrome-<port>` profile arguments. Tracked sessions use persisted idle time; untracked roots use process age as a conservative fallback.

## Risks

- PID reuse or accidental signaling: fail closed unless command line, port, profile, and root-process shape all match.
- Active long-lived untracked browser: dashboard mutation is removed to prevent new untracked roots; existing untracked roots retain the documented 48-hour process-age grace.
- Concurrent cleaners: serialize with the existing session-file flock and revalidate immediately before signaling.
