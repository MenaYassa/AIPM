# Contributing to AIPM

This document describes how to develop, validate, and submit changes
to the AIPM repository. It records the repository's local validation
contract and how it relates to CI, so a clean checkout behaves the
same in both places.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) (the repository ships a tracked,
  frozen `uv.lock`; CI installs from it)
- Git

## Setup

From a checkout:

```bash
uv sync --frozen --extra dev
```

This creates a `.venv` with the package installed (editable) and the
`dev` extra (`pytest>=8`). The lockfile is authoritative: CI installs
with `--frozen` and does not resolve dependencies, so a change to
`pyproject.toml` dependencies must be accompanied by a regenerated
`uv.lock` (`uv lock`) in the same change.

An equivalent local setup without uv is
`python -m pip install -e '.[dev]'` (see `README.md`), but uv is what
CI uses and is the recommended path.

## Running the tests

```bash
.venv/bin/python -m pytest -p no:cacheprovider -q
```

(or `pytest -q`, per `README.md`, if the venv is active). The suite is
self-contained: it uses disposable temporary repositories and
directories for Git, Docker, Compose, SQLite, and update-transaction
coverage and does not require a Docker daemon, network access, or any
path outside the checkout's `tmp_path` fixtures.

### Known host-state-dependent tests

Two tests observe the *host's* configured users and sudo policy and
can fail on a development machine that does not match the production
identity contract:

- `tests/test_mc612_stage15_privilege.py::test_assert_detects_human_session_without_executor_rule`
- `tests/test_mc612_stage25a_identity_setup.py::test_s6_privileged_group_contamination_fails`

They assert fail-closed privilege-boundary behavior (the operator's
personal account must not hold broad sudo, and the `aipm` user must
not belong to privileged groups such as `docker`). On hosts where the
operator has passwordless broad sudo or the `aipm` user exists with
extra group memberships, the fail-closed detection fires and these
tests report the host's non-conforming state as a failure. Clean
runners (including CI) do not have an `aipm` user or broad
passwordless sudo, so the full suite passes there unchanged.

CI therefore runs the **full suite with no exclusions**. Do not add
`--deselect`, `--ignore`, skips, or `continue-on-error` to work
around a failure; fix the cause or document the host-state dependency
here.

## Release validation

The repository carries its own release-tooling contract:

```bash
.venv/bin/python ops/validate-release.py --development  # development mode
.venv/bin/python ops/build-release.py                   # build metadata + manifest
.venv/bin/python ops/validate-release.py                # production mode
```

`build-release.py` requires a clean tracked tree (uncommitted staged
or unstaged changes abort it) and writes gitignored outputs
(`build_meta.json`, `release-manifest.json`); production-mode
validation additionally requires those outputs and that
`build_meta.json`'s `commit_sha` equals `HEAD`. CI runs this exact
chain after the test suite on every push to `main` and every pull
request, so a change that passes CI is release-valid from a clean
checkout.

## CI

`.github/workflows/ci.yml` is validation-only: it checks out the
repository, installs the locked dependencies, runs the full test
suite, and runs the release-validation chain. It never publishes,
deploys, or touches anything outside the runner. Its posture
(pinned action SHAs, `permissions: contents: read`,
`persist-credentials: false`, no secrets, no `pull_request_target`,
no `continue-on-error`) is enforced by `tests/test_ci_workflow.py` —
if you intentionally change the workflow or an action pin, update
the pinned-action expectations in that test file in the same change.

## Commit conventions

- Keep changes minimal and scoped: one logical change per commit,
  no unrelated cleanup mixed in.
- Conventional-commit style prefixes are used in this repository's
  history (`feat:`, `fix:`, `test:`, `refactor:`, `docs:`).
- Never commit secrets, tokens, host-specific credentials, generated
  environments, or runtime state (`state/`, `reports/`, `logs/`).
  `.gitignore` covers the generated release outputs and local
  scratch; it is not a substitute for reviewing what you stage.
- Validation before submitting: full test suite plus the release
  chain above must pass from a clean checkout.

## What is out of scope for contributions

The MC-6.12 execution plane (executor, production action execution)
and MC-6.13 live orchestration are gated by their own roadmap
authorization; do not implement them speculatively. Deployment and
live-VPS operations are not part of repository work.
