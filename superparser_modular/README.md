# Pokemon Product Monitor backend

The backend is a Python 3.11+ FastAPI application containing the retailer parsers, SQLite persistence, local dashboard API, background workers, notifications, and optional Selenium-assisted actions.

## Local setup

The repository-root `install_dependencies.bat` creates `.venv` and installs the hash-locked dependencies. For a manual setup, install `../requirements.lock` and then install this package into a Python 3.11 or 3.12 virtual environment.

Copy `.env.example` to `.env`. For the first run, keep:

- `ACTION_MODE=off`;
- every retailer flag disabled;
- notifications and debug-file logging disabled;
- proxy, prewarm, and warm-tab features disabled;
- every credential blank.

Prefer an absolute `DB_FILE` path in a private data directory outside the repository.

From this directory:

```powershell
.\.venv\Scripts\python.exe -m pokemon_parser.cli.main --init-db
.\.venv\Scripts\python.exe -m pokemon_parser.cli.main --once
```

The repository-root launcher binds the API to `127.0.0.1:8000`. The API has no general-purpose authentication layer and must not be exposed directly to a LAN or the internet.

## Tests

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
```

## Runtime data boundary

`.env`, `filters.json`, `scan_settings.json`, databases, backups, browser profiles, cookies, logs, screenshots, page captures, settings snapshots, and checkout diagnostics are private runtime data. Never commit them, attach them to public issues, place them in build contexts, or include them in release archives.

See [data handling](../docs/DATA_HANDLING.md), the [security policy](../docs/SECURITY.md), and the [deployment runbook](../docs/DEPLOYMENT.md).
