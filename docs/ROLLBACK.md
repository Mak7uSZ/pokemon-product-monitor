# Rollback Runbook

## Rollback triggers

Rollback if migration integrity is not `ok`, row counts materially diverge, readiness remains unhealthy, the dashboard cannot load persisted state, duplicate actions appear, warm-tab ownership duplicates, or error/disk growth is uncontrolled.

## Immediate containment

1. Set `ACTION_MODE=off` and `ENABLE_NOTIFICATIONS=false` if the API remains reachable.
2. Run `stop_dashboard.bat`; verify the backend, parser, Selenium worker, ChromeDriver, and app-owned Chrome processes have stopped.
3. Preserve logs and the failed database as private incident evidence. Do not commit or upload them to a public issue.

## Application-only rollback

Use this only when the database schema/data are known compatible with the previous release.

1. Switch to the last accepted release commit.
2. Run `install_dependencies.bat` to reinstall its locked environment and rebuild its frontend.
3. Keep `ACTION_MODE=off`, start the service, and require both health endpoints plus a no-side-effect scan before re-enabling anything.

## Database rollback

An older application must not be pointed at a newer schema unless compatibility was explicitly tested. Restore the pre-upgrade database together with the previous application release.

1. With all processes stopped, rename the failed database and any `-wal`/`-shm` sidecars to a private incident location.
2. Verify the selected pre-migration/online backup on a disposable path with `PRAGMA integrity_check`; confirm its expected schema and row counts.
3. Restore using the SQLite online backup helper, not a raw copy of a database that may be live:

   ```powershell
   $env:PYTHONPATH = "$PWD\superparser_modular\src"
   python -c "from pathlib import Path; from pokemon_parser.storage.backup import backup_sqlite_database; backup_sqlite_database(Path(r'C:\private\verified-backup.db'), Path(r'C:\private\production.db'))"
   ```

4. Restore the matching private `.env`/scan settings snapshot if configuration changed. Never enable `ALLOW_LEGACY_BACKUP_RESTORE` as a shortcut during normal rollback.
5. Start the previous release with side effects off. Require liveness, readiness, integrity, expected counts, dashboard state, restart persistence, and a no-side-effect scan.

## Secret found in published history

If a secret is found in Git history, revoke it immediately and stop publishing releases. Restrict repository visibility when that reduces ongoing exposure, remove affected artifacts, inspect every reachable ref, and follow the hosting provider's sensitive-data-removal process. Rebuild from a verified clean source root when the affected objects cannot be safely excluded. Assume public copies and clones cannot be recalled.

## Acceptance record

Record the release and rollback commit hashes, database backup path and checksum, schema version, integrity result, row counts, health responses, action mode, operator, and UTC timestamps. Retain that record outside the public repository.
