# Deployment runbook

## Supported topology

Run the application on one trusted Windows host and bind it only to `127.0.0.1`. The dashboard exposes administrative operations and has no general-purpose authentication layer, so direct LAN or internet exposure is unsupported.

If remote access is required, use a private VPN or an authenticated TLS reverse proxy and complete a separate authorization, session, and CSRF review first.

## Host preparation

- Install Python 3.11 or 3.12 and Node.js 24 with npm.
- Install Chrome only if Selenium workflows are needed.
- Use a dedicated Windows account and restrict its private data directory to that account.
- Keep enough free space for the live database, a verified backup, and normal growth.
- Clone a reviewed release commit and run `install_dependencies.bat`.
- Copy `superparser_modular/.env.example` to `.env`.
- Start with `ACTION_MODE=off`, every retailer disabled, notifications disabled, and credentials blank.

## Database and configuration

1. Stop every existing parser and backend process.
2. Create and verify a SQLite online backup before an upgrade. Keep it outside the repository.
3. Set `DB_FILE` to an absolute path in the private data directory. Do not raw-copy a live WAL database.
4. From `superparser_modular`, run:

   ```powershell
   .\.venv\Scripts\python.exe -m pokemon_parser.cli.main --init-db
   ```

   This initializes or migrates the schema, creates a verified pre-migration backup when required, prints the startup preflight, and exits without scanning.
5. Confirm that initialization reports a healthy database before starting the service.

## Start and acceptance

1. Run `start_dashboard.bat`.
2. Confirm that `http://127.0.0.1:8000/health/live` returns HTTP 200 and `status=alive`.
3. Confirm that `http://127.0.0.1:8000/health/ready` returns HTTP 200 with healthy database and frontend checks.
4. Confirm that the dashboard loads and credentials remain masked.
5. With `ACTION_MODE=off`, run a controlled scan and verify that no notification or browser action is dispatched.
6. Restart the backend and browser, then confirm that expected products, filters, and watchlist state persist without duplicates.
7. Enable one retailer at a time and observe its cooldown and failure behavior.
8. Select `notify_only` or `selenium` only after the monitor-only workflow is accepted.

Use a dedicated least-privilege retailer account and a limited payment method for Selenium mode. Treat any unattended purchasing workflow as production automation with real financial consequences.

## Monitoring and maintenance

- Probe `/health/live` for process liveness and `/health/ready` for database and frontend readiness.
- Review the local dashboard status and private logs for parser failures, challenge cooldowns, queue depth, stale checks, and database growth.
- Keep logs and checkout timelines private even when redaction is enabled.
- Define local retention limits for database history, backups, settings snapshots, logs, and diagnostics.
- Create and verify an online backup before every upgrade.
- Periodically test restoration on a disposable copy.

## Release verification

A release candidate should pass:

```powershell
python scripts\release_guard.py --tracked
$env:PYTHONPATH = "$PWD\superparser_modular\src"
.\superparser_modular\.venv\Scripts\python.exe -m pytest superparser_modular\tests -p no:cacheprovider -q
Set-Location frontend
npm ci
npm audit --audit-level=low
npm test
npm run build
```

Also scan the complete reachable Git history for secrets and inspect the exact release archive rather than archiving a working directory. Follow [ROLLBACK.md](ROLLBACK.md) if an acceptance gate fails.
