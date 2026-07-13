# Security policy

## Supported code

Security fixes are made on the current default branch. Older branches and releases are unsupported unless a maintainer says otherwise.

This project is intended for a single trusted operator and a loopback-only dashboard. It is not an authenticated multi-user service. Direct LAN or internet exposure is unsupported.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature under the repository's **Security** tab. Do not open a public issue containing vulnerability details, credentials, databases, logs, screenshots, captured pages, browser profiles, checkout information, or an exploit proof that uses real data.

If private reporting is temporarily unavailable, open a minimal public issue asking the maintainer to enable a private reporting channel. Do not include security details in that issue.

A useful private report includes:

- the affected commit, version, and component;
- impact and required preconditions;
- a minimal reproduction using synthetic data;
- whether credentials, sessions, or personal data may be exposed;
- a proposed mitigation, if known.

Never send a live secret. Immediately revoke any credential accidentally included in a report.

The maintainers aim to acknowledge a report within 5 business days and provide initial triage within 10 business days. Disclosure timing depends on severity and remediation readiness.

## Security expectations

- `ACTION_MODE=off`, retailer parsers, notifications, browser prewarm, warm tabs, and debug-file logging are disabled by default.
- `ACTION_MODE=selenium` can add products to carts, submit checkout details, and potentially place orders. Use dedicated accounts, spending controls, a limited payment method, and an attended test.
- Credentials are stored locally in `.env`; dashboard credential fields are write-only. This is not a secrets manager. Restrict filesystem access and never share the runtime directory.
- Keep the dashboard on `127.0.0.1`. Remote deployment requires an authenticated TLS gateway or private VPN plus a separate authorization, session, CSRF, and threat-model review.
- Debug logs and diagnostics remain private even when redaction is enabled. Redaction reduces risk; it does not make artifacts safe to publish.
- Respect retailer rate limits, cooldown and challenge signals, robots directives, account terms, and applicable law. Do not weaken challenge detection or conceal automation.

## Secret or personal-data incident response

1. Stop publication and deployment, then preserve only the minimum evidence in a restricted location.
2. Revoke the affected token, webhook, key, password, payment credential, or browser session. Deleting a file or rewriting Git history does not revoke a copied credential.
3. Make affected repositories and artifacts private when that reduces ongoing exposure; assume existing clones and caches remain.
4. Identify affected commits, branches, tags, pull requests, artifacts, caches, releases, packages, mirrors, clones, archives, backups, and pasted diagnostics.
5. Remove data from systems under the maintainer's control and request deletion from service providers where appropriate.
6. Rebuild from a verified clean source snapshot if published history may be contaminated.
7. Re-run repository guards, secret scanners, personal-data review, tests, archive inspection, and clean-clone verification before resuming.
8. Notify affected people or providers and obtain privacy or legal advice when personal or regulated data may be involved.

Security advisories should explain impact and remediation without publishing live credentials, session material, personal data, or unnecessary exploit details.
