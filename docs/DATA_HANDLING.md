# Data handling and publication boundary

This application is designed for one trusted operator on a local machine. Its
source can be public, but its runtime state cannot. The supported dashboard
binds to loopback and does not provide a general-purpose authentication layer.

## Classification

- **Public**: source, documentation, deterministic synthetic fixtures, public
  retailer URLs, and non-secret configuration names.
- **Internal**: operational preferences and diagnostic metadata that are not
  credentials but reveal how an installation is operated.
- **Confidential**: product histories, watchlists, failures, screenshots,
  response bodies, and other installation-specific records.
- **Credential**: tokens, passwords, webhook URLs, proxy credentials, private
  keys, and deployment or package-registry credentials.
- **Personal data**: names, email addresses, postal addresses, account IDs,
  chat IDs, and order/customer details.
- **Session material**: cookies, browser profiles, local/session storage,
  authorization headers, refresh tokens, and authenticated caches.
- **Production-only data**: live databases, backups, logs, captures, action
  timelines, and migration working files. These are never fixtures.

## Data-flow inventory

The “risk” column covers accidental commits, log disclosure, and release or CI
artifact inclusion. A value being write-only in the dashboard does not make
the underlying `.env` file safe to share.

| Data | Origin → storage or transmission | Class | Publication and logging risk | Required control |
|---|---|---|---|---|
| Environment variables | Operator/dashboard → process environment and `superparser_modular/.env` | Credential, personal data, internal | `.env` can contain every sensitive category; stack traces or diagnostics can reveal paths | Commit only `.env.example` with blank placeholders. Restrict file ACLs. Never upload `.env`; rotate values after suspected exposure. |
| Telegram token and chat ID | BotFather/operator → `.env` → Telegram HTTPS API | Credential, personal data | Token appears in the request URL; chat ID and message content may enter logs or captures | Keep notifications off by default. Redact URLs and structured values. Revoke exposed tokens and review bot/webhook history. |
| Retailer/product URLs and IDs | Public retailer pages and operator watchlists → SQLite, logs, API/dashboard | Public when generic; internal when tied to an operator | Watchlists reveal interests and purchase intent; query strings may contain identifiers | Public examples must use synthetic IDs. Strip query credentials and session parameters. Treat real watchlists as confidential. |
| Account credentials and auth tokens | Operator/browser/login flows → `.env`, browser profile, retailer endpoints | Credential, session material | May exist in form state, cookies, browser databases, screenshots, HTML, or driver logs | Never persist in fixtures. Use dedicated least-privilege accounts. Invalidate sessions and rotate passwords after exposure. |
| Cookies, local storage, session storage | Retailer and dashboard browser sessions → Chrome profile and browser storage | Session material | Profile databases are directly reusable; dashboard storage can reveal preferences/status | Store profiles outside the repository. Never copy profiles into support bundles. The dashboard clears its own storage only; that does not invalidate retailer sessions. |
| Checkout contact data | Operator → `.env` → retailer checkout forms | Personal data | Names, email, address, and account details can appear in DOM, screenshots, HTML, traces, and crash output | Keep action mode off unless explicitly needed. Use write-only API fields, private file ACLs, artifact opt-in, and short retention. |
| Payment-related data | Operator → `.env` → retailer/payment forms | Credential-like payment data, personal data | Card number, expiry, CVV, cardholder name, iframe/DOM diagnostics | Prefer retailer-stored or dedicated limited payment methods. Never log or back up fields. Never collect CVV in diagnostics. Reissue exposed payment credentials through the issuer. |
| Proxy configuration | Operator → `.env` → HTTP, Telegram, and browser networking | Internal; login/password are credentials | Hostnames reveal infrastructure; credentials may appear in URLs or errors | Keep disabled by default. Redact authentication. Exclude `.env` and settings files. Rotate proxy credentials after exposure. |
| SQLite product records | Retailer parsers → `products` | Confidential, production-only | Titles, URLs, prices, availability history, raw/extra JSON, errors | Use an empty migrated database for tests. Never commit or raw-copy a live WAL database. |
| Filters | Operator/dashboard or legacy `filters.json` → `filters` | Internal, production-only | Reveals monitored products and spending limits | `filters.json` is ignored. Publish only disabled `filters.example.json` with synthetic data. |
| Watchlist | Parser/operator → `priority_watchlist` | Confidential, production-only | Contains product IDs, URLs, prices, status history, matching rules, and errors | Exclude DB and settings snapshots. Export only when necessary and handle as confidential. |
| Events and action history | Pipeline/workers → `events`, `action_log`, `purchase_state` | Confidential, production-only | Can reveal purchase intent, checkout outcome, confirmation URLs, or failure details | Redact before insertion, keep DB private, and do not use production rows as tests or documentation examples. |
| Runtime status/log records | Backend/workers → `runtime_logs`, console, API | Internal or confidential | Error messages and structured details may contain URLs, paths, identifiers, or response fragments | API responses are `no-store`; redact secrets; bind to loopback; do not attach raw status dumps publicly. |
| Debug logs | Logging subsystem → `debug_logs/*.log` | Confidential, production-only | Includes local paths, parser decisions, product IDs, timing, and exceptions | File logging is off by default. When enabled, use bounded rotation, private ACLs, explicit retention, and secure deletion. Never publish. |
| Screenshots and page HTML | Selenium/watchlist diagnostics → `debug_artifacts/**` | Confidential, personal data, session material | Pages can contain names, cart contents, addresses, tokens, cookies embedded in markup, or anti-bot IDs | Diagnostics require explicit opt-in where supported. Quarantined challenge pages are not captured. Review and delete locally; never release. |
| GraphQL/API response bodies | Retailer HTTP clients → memory; optional diagnostic HTML/JSON; logs on failure | Confidential or public source content; may contain session material | Bodies and headers can include request IDs, experiments, account state, or copyrighted page content | Log outcome codes and bounded metadata, not bodies/headers. Use hand-built synthetic response objects in tests. |
| Crash and Selenium diagnostics | Runtime → `debug_logs` snapshots and driver logs | Confidential, internal | Contains absolute paths, process details, browser options, profile locations, and tracebacks | Keep file logging off unless diagnosing. Redact, store outside source, and never upload without a second-person review. |
| Database backups | Migration/manual backup → `backups/` or operator-selected location | Confidential, production-only | A backup is as sensitive as the live database and may be much larger than expected | Create with the SQLite backup API, verify integrity, encrypt at rest, and keep outside the repository and cloud-sync shares. |
| Settings backups | Dashboard → download or `settings_snapshots/*.json` | Internal/confidential, production-only | Credentials are excluded, but filters, watchlists, proxy host, branch, and operating preferences remain | Treat as confidential. Validate imports; never commit snapshots or attach them publicly. |
| Temporary/migration files | Atomic writes and SQLite → `.tmp`, journals, WAL/SHM, migration lock | Confidential, production-only | Can contain complete or partial copies of private data | Keep in private runtime directory, exclude from Git/Docker/packages, and clean only with the migration safety rules. |
| Frontend storage | Dashboard → browser `localStorage`, `sessionStorage`, cookies | Internal; potentially session material | Theme/synchronization state is low sensitivity, but future fields could expand | Do not store credentials. Clear on clean-slate recovery. Treat browser profiles as private regardless. |
| API responses | SQLite/config/runtime → loopback HTTP → dashboard | Internal/confidential | Credentials are write-only, but product/log/status routes expose operational data | Keep trusted-host and restrictive CORS controls. Do not bind directly to LAN/internet; add authenticated TLS gateway only after a separate review. |
| CI logs, caches, artifacts | Source/PR code and dependency tools → GitHub Actions | Internal/public | Malicious PR code can print environment data; artifacts can unintentionally package the workspace | Give workflows read-only permissions, no production secrets, no `pull_request_target`, pinned actions, and no broad artifact upload. Review any future upload allowlist. |
| Source/build archives | Approved commit → `git archive`, frontend build, package, Docker context | Public | A broad filesystem archive can collect ignored runtime data | Build only from a clean exported commit. Scan the exact archive and extracted tree; never zip the working directory. |

The application sends outbound requests to the configured retailer sites,
Telegram when enabled, and a proxy when enabled. It has no intentional product
telemetry. Dependency tools and the browser may have their own network behavior;
run release builds in an isolated environment with no production credentials.

## Public source allowlist

The public repository may contain source, migrations, tests, hash-locked
dependency manifests, the built dashboard required by the local backend,
documentation, `.env.example`, and deterministic synthetic fixtures.

The following never belong in a public source tree or build context:

- real environment/configuration files, filters, scan settings, or watchlists;
- databases, journals, WAL/SHM files, backups, snapshots, or migration locks;
- browser profiles, cookies, storage, account caches, or driver logs;
- screenshots, captured pages/responses, action timelines, crash dumps, or logs;
- credentials, private certificates/keys, webhooks, proxy details, checkout data;
- ZIP/bundle exports, old clones, repository bundles, or cloud-sync copies.

`.gitignore` is convenience, not a security boundary. `.dockerignore`, the
packaging allowlist, `scripts/release_guard.py`, Gitleaks, and an inspected
clean export are independent controls.

## Synthetic fixtures

Fixtures must be authored from a minimal schema/selector description, not by
redacting a captured production page. Use reserved `.test`/`.invalid` domains,
obviously synthetic titles, synthetic IDs, non-personal example addresses, and
disabled actions. A fixture must contain no scripts, tracking tags, headers,
cookies, account state, unique production identifiers, or unrelated page
content.

Fixture review requires:

1. explain which parser behavior the smallest fixture proves;
2. run `python scripts/release_guard.py --tracked`;
3. run Gitleaks and the relevant parser tests;
4. search the diff for production product IDs, names, emails, addresses,
   cookies, authorization fields, and absolute paths;
5. have a second reviewer confirm the data was synthesized rather than merely
   redacted.

An empty database should be created through migrations at test/runtime start.
No production database, database subset, or copied record is an acceptable
fixture.

## Retention and disposal

Keep runtime data only as long as operationally required. Define local limits
for database history, settings snapshots, verified migration backups, debug
logs, and diagnostic captures. Stop the application before disposing of live
SQLite sidecars or browser profiles. Use the operating system’s secure storage
and deletion facilities as appropriate; ordinary deletion may not recall cloud
sync versions or backups. Record credential revocations separately from file
deletion because deleting a token copy does not revoke the token.
