# Pokemon Product Monitor

[![CI](https://github.com/Mak7uSZ/pokemon-product-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/Mak7uSZ/pokemon-product-monitor/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A local-first monitor for Pokemon product availability. It combines a Python/FastAPI backend, SQLite persistence, retailer-specific parsers, optional Selenium-assisted actions, and a React dashboard.

The supported setup is one trusted operator on a Windows computer. The dashboard binds to `127.0.0.1` and is not designed for direct LAN or internet exposure.

## Features

- Monitors MediaMarkt, DreamLand, bol.com, and PocketGames.
- Stores products, scan history, filters, and a priority watchlist in SQLite.
- Preserves the last known-good inventory state when a retailer check fails.
- Provides health checks, runtime status, settings, backups, and diagnostics through a local dashboard.
- Supports notifications and optional browser-assisted actions with explicit opt-in controls.
- Defaults every retailer and side-effecting feature to disabled.

## Requirements

- Windows 10 or 11
- Python 3.11 or 3.12
- Node.js 24 and npm
- Google Chrome, only when Selenium features are enabled
- Enough free disk space for the database and a verified pre-migration backup

## Quick start

1. Clone the repository and open a terminal in its root.
2. Install the locked dependencies and build the dashboard:

   ```powershell
   .\install_dependencies.bat
   ```

3. Create a private runtime configuration:

   ```powershell
   Copy-Item superparser_modular\.env.example superparser_modular\.env
   ```

4. Leave `ACTION_MODE=off`, every `ENABLE_*` retailer flag disabled, notifications disabled, and credentials blank for the first start.
5. Initialize the database:

   ```powershell
   Set-Location superparser_modular
   .\.venv\Scripts\python.exe -m pokemon_parser.cli.main --init-db
   Set-Location ..
   ```

6. Start the application:

   ```powershell
   .\start_dashboard.bat
   ```

7. Open [http://127.0.0.1:8000/clean-slate?source=launcher](http://127.0.0.1:8000/clean-slate?source=launcher) and check:

   - [liveness](http://127.0.0.1:8000/health/live)
   - [readiness](http://127.0.0.1:8000/health/ready)

Use `stop_dashboard.bat` for a normal stop. After the first successful run, `start_dashboard_hidden.vbs` can launch the owner dashboard without a console window.

## Safe configuration

The example configuration is intentionally fail-safe. Review these controls before enabling a parser or action:

| Setting | Safe initial value | Purpose |
|---|---:|---|
| `ACTION_MODE` | `off` | Prevents notification and browser action dispatch. |
| `ENABLE_MEDIAMARKT` | `false` | Enables MediaMarkt monitoring. |
| `ENABLE_DREAMLAND` | `false` | Enables DreamLand monitoring. |
| `ENABLE_BOL` | `false` | Enables bol.com monitoring. |
| `ENABLE_POCKETGAMES` | `false` | Enables PocketGames monitoring. |
| `ENABLE_NOTIFICATIONS` | `false` | Enables Telegram notifications when credentials are configured. |
| `POKEMON_DEBUG_LOGS` | `0` | Keeps diagnostic file logging disabled. |
| `PROXY_ENABLED` | `false` | Disables proxy routing. |
| `SELENIUM_PREWARM` | `false` | Prevents background browser startup. |
| `WATCHLIST_WARM_TABS_ENABLED` | `false` | Prevents persistent retailer tabs. |
| `ALLOW_LEGACY_BACKUP_RESTORE` | `false` | Prevents implicit restoration of legacy backups. |

Prefer an absolute `DB_FILE` path in a private data directory outside the clone. Keep the generated `.env` readable only by the account that runs the application.

### Action modes

- `off`: monitor only; no notification or purchase workflow is dispatched.
- `notify_only`: permits configured notifications but no Selenium purchase workflow.
- `selenium`: permits browser-assisted actions and can create real retailer-side effects.

Treat `selenium` as an attended production mode. Use dedicated least-privilege accounts, spending controls, and a limited payment method. Test with non-purchasing settings first.

## Project layout

```text
.
|-- frontend/                 React/Vite dashboard
|-- scripts/                  release and repository safety checks
|-- superparser_modular/
|   |-- src/pokemon_parser/   FastAPI app, parsers, storage, and workers
|   |-- tests/                backend test suite
|   `-- .env.example          safe configuration template
|-- docs/                     operations, security, and data guidance
|-- install_dependencies.bat
|-- start_dashboard.bat
`-- stop_dashboard.bat
```

The backend serves the production frontend bundle from `frontend/dist`. For frontend development, run Vite separately on loopback.

## Development and verification

Backend tests:

```powershell
$env:PYTHONPATH = "$PWD\superparser_modular\src"
.\superparser_modular\.venv\Scripts\python.exe -m pytest superparser_modular\tests -p no:cacheprovider -q
```

Frontend checks:

```powershell
Set-Location frontend
npm ci
npm audit --audit-level=low
npm test
npm run build
```

Repository safety check:

```powershell
Set-Location ..
python scripts\release_guard.py --tracked
```

CI repeats the release guard, locked dependency installs, Python and JavaScript tests, dependency audits, frontend build, and full-history secret scanning.

## Private runtime data

Never commit or upload any of the following:

- `.env` files or real credentials;
- SQLite databases, WAL/journal files, backups, or settings snapshots;
- browser profiles, cookies, local storage, or authenticated sessions;
- logs, screenshots, captured HTML/API responses, crash dumps, or checkout diagnostics;
- real names, addresses, account identifiers, watchlists, or payment details.

Ignored files are still private data. Review release archives and support attachments independently of Git.

## Documentation

- [Security policy](docs/SECURITY.md)
- [Data handling and publication boundary](docs/DATA_HANDLING.md)
- [Security requirements for contributors](docs/CONTRIBUTING_SECURITY.md)
- [Deployment runbook](docs/DEPLOYMENT.md)
- [Rollback runbook](docs/ROLLBACK.md)
- [Backend notes](superparser_modular/README.md)

## Responsible use

Retailer sites, account providers, and local laws may restrict automated access or purchasing. Respect rate limits, challenge signals, robots directives, account terms, and applicable law. Do not use this project to bypass access controls or conceal automation.

## License

Copyright 2026 Mak7uSZ.

Licensed under the [Apache License 2.0](LICENSE).
