# Security Policy

## Scope

This policy covers the AIPM repository: the CLI, the Mission Control
dashboard code, the MC-6.12 control plane, the update transaction
pipeline, and the release tooling under `ops/`. It does not cover the
live VPS deployment; live infrastructure is operated separately and is
not modified through repository changes.

## CI trust boundary

CI (`.github/workflows/ci.yml`) is validation-only and is designed to
be safe against untrusted pull requests:

- Actions are pinned to full commit SHAs (verified by
  `tests/test_ci_workflow.py`).
- The workflow declares `permissions: contents: read`, checks out
  with `persist-credentials: false`, uses no secrets, and is triggered
  only by `push` to `main` and `pull_request` — never
  `pull_request_target`, which would run workflow code from the pull
  request's own ref with repository context.
- Run steps contain no publish, deploy, or network-egress commands;
  everything runs against the freshly checked-out tree on an
  ephemeral runner.
- `tests/test_ci_workflow.py` fails the suite if this posture drifts
  (new unpinned actions, added secrets, `continue-on-error`, excluded
  tests, or publish/deploy commands).

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner
(MenaYassa). Do not open a public issue describing an exploitable
weakness in the transport, session, CSRF, rate-limiting, privilege,
audit, or update-transaction paths. Include reproduction steps and,
where possible, the specific contract you believe is violated
(`docs/MC-6.12_*.md` documents the security-relevant contracts).

## Security-relevant contracts

- **Operator transport** (`docs/MC-6.12_OPERATOR_TRANSPORT.md`):
  localhost-only bind, session cookies, CSRF, bounded 429 rate
  limiting.
- **Privilege boundary** (`docs/MC-6.12_PRIVILEGE_BOUNDARY.md`): the
  operator's personal account must not hold broad passwordless sudo;
  the `aipm` user must not belong to privileged groups (sudo, docker,
  admin, root, wheel).
- **Update transactions** (`PRODUCTION_ROADMAP.md` and the update
  suite): dry-run never mutates state; approval is explicit; stashes
  are preserved on conflicts; no destructive Git commands run in the
  transaction.
- **Redaction**: audit records, logs, and API responses redact
  secrets; channel credentials are referenced via environment-variable
  names and never stored in Git, SQLite, logs, or payloads.

## No secrets in the repository

Keys, tokens, host-specific secrets, generated environments, and
runtime state must never be committed. `.gitignore` excludes the
generated release outputs (`build_meta.json`,
`release-manifest.json`), `.env*`, logs, and local scratch files, and
`ops/validate-release.py` fails release validation if forbidden
artifacts (`AGENTS.md`, `commands.txt`, model weights) or tracked
runtime-state directories appear in the tree.

## Execution-plane status

The production executor is intentionally unimplemented and denied:
production actions are refused, the production kill switch is
permanently engaged, and the action API is not exposed. Any code that
changes this posture requires explicit, separate authorization — do
not implement it speculatively.
