# Validation

- `cd web && uv run python -m unittest discover -s tests -v`: 25 tests passed.
- `cd web && uv run python -m compileall -q src tests`: passed.
- `cd web && uv build`: source distribution and wheel built.
- `git diff --check`: passed.
- Real `/proc` discovery: all five managed Chromium roots recognized with correct ports.
- `systemctl --user start openeyes-web-cleanup.service`: status 0/SUCCESS.
- `systemctl --user list-timers openeyes-web-cleanup.timer`: enabled, active, next run scheduled 30 minutes later.
- `systemd-analyze --user verify ...`: OpenEyes units valid; unrelated installed Spice unit emitted an existing warning.
- 1128 Compose project: no running containers remain.
- Memory after cleanup: 35 GiB used, 85 GiB available (was 42 GiB used, 79 GiB available).

No browser UI change was made; screenshot QA is not applicable.
