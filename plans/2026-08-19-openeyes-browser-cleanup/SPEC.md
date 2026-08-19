# OpenEyes browser cleanup

## Problem

OpenEyes intentionally detaches managed Chromium processes so sessions survive MCP reconnects. Cleanup currently runs only inside an MCP process, while the dashboard can delete lifecycle records after one transient 300 ms health-check failure. Live browsers then become untracked and can persist indefinitely; port allocation can also reuse their live ports.

## Scope

- Preserve detached browser/session behavior.
- Stop dashboard reads from deleting lifecycle records.
- Prevent allocation of ports that already have a live listener.
- Add a standalone, conservative cleanup command for tracked expired sessions and old untracked OpenEyes browser roots.
- Run that command periodically through a user-level systemd timer on this host.
- Safely verify process ownership before signaling any PID.

## Acceptance

- Given a transient dashboard health-check failure, when sessions are listed, then the lifecycle record remains persisted.
- Given an unregistered live CDP listener, when a session allocates a port, then that port is skipped.
- Given a tracked session idle longer than 48 hours, when standalone cleanup runs, then only its verified OpenEyes Chromium root is signaled and its record is removed after successful reclamation.
- Given an untracked managed Chromium root younger than 48 hours, when cleanup runs, then it remains alive.
- Given an untracked managed Chromium root older than 48 hours, when cleanup runs, then it is reclaimed after exact command-line/profile verification.
- Given a stale/reused PID that does not match the expected OpenEyes port and profile, when cleanup runs, then it is not signaled.
- Given the host user session is running, periodic cleanup executes every 30 minutes without requiring an MCP client.

## Non-goals

- Killing current browsers merely because their parent MCP process exited.
- Deleting browser profile data or login state.
- Changing the 48-hour retention policy.
- Refactoring unrelated browser or dashboard behavior.
