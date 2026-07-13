# Security requirements for contributors

Every contribution must preserve the boundary between publishable source and
private runtime data. A clean diff is required even when Git says a sensitive
file is ignored.

## Never contribute

- `.env` or configuration containing a real value;
- databases, WAL/journal files, backups, settings snapshots, or copied rows;
- browser profiles, cookies, local/session storage, tokens, or account caches;
- logs, screenshots, captured HTML/GraphQL/API bodies, HAR files, crash dumps,
  checkout timelines, or support bundles;
- real names, emails, addresses, payment data, chat/account IDs, watchlists, or
  unique identifiers copied from an operator’s diagnostics;
- archives of a working directory, old Git bundles, vendored `node_modules`,
  virtual environments, or generated dependency caches;
- unreviewed commits, patches, or generated files copied from another source.

If sensitive data enters a commit, stop. Revoke affected credentials/sessions,
notify the maintainer privately, and rebuild the contribution from a known-safe
parent. Do not assume that amending a commit recalls a pushed copy.

## Required local checks

From the repository root:

```powershell
git status --short
git diff --check
python scripts/release_guard.py --tracked
$env:PYTHONPATH = "superparser_modular/src"
python -m pytest superparser_modular/tests -p no:cacheprovider -q
Set-Location frontend
npm ci
npm audit --audit-level=low
npm test
npm run build
```

Run Gitleaks on full reachable history before proposing a release or after any
history import. CI repeats the prohibited-file guard, backend/frontend gates,
dependency review, and Gitleaks.

## Safe fixtures and examples

Create the smallest deterministic fixture by hand. Use reserved `.test` or
`.invalid` domains, synthetic product IDs/titles, non-personal placeholder
values, and disabled actions. Do not save a live page and redact it; hidden
scripts, headers, experiments, session state, or identifiers are easy to miss.

Tests that need credential-shaped values must construct obviously synthetic
values at runtime so repository scanners do not need broad allowlists. No
allowlist may suppress a real secret pattern solely to make CI green.

Configuration documentation may show variable names, but secret fields must be
blank or use an unmistakable placeholder. Examples must keep `ACTION_MODE=off`
and all network/parser flags disabled unless the example is specifically
explaining opt-in behavior.

## Dependency and workflow changes

- Update the lockfile together with the manifest and preserve Python hashes.
- Review new direct and transitive licenses, maintainers, install scripts,
  advisories, and provenance.
- Pin GitHub Actions to a full commit SHA with a version comment.
- Keep workflow permissions read-only unless a reviewed job has a narrow need.
- Do not use `pull_request_target` to execute contributor code.
- Do not add production secrets to test/build workflows or execute an
  untrusted artifact in a privileged release/deployment job.
- Any artifact upload must use an explicit allowlist and pass the release guard
  on the staged artifact directory first.

## Pull-request review

Reviewers must inspect the complete changed-file list, including renamed,
binary, generated, and deleted files. Confirm that:

1. the change uses synthetic data;
2. configuration remains fail-safe;
3. log/error paths redact rather than serialize sensitive inputs;
4. diagnostic output stays opt-in, bounded, private, and ignored;
5. external requests are documented and respect cooldowns/rate limits;
6. action-mode changes cannot cause an unapproved purchase;
7. tests and the release guard pass without weakening their rules;
8. imported history or cherry-picked changes do not introduce private data.

Report security concerns through the private process in
[`SECURITY.md`](SECURITY.md), not in a public review thread.
