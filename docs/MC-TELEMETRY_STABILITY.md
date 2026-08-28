# Telemetry Stability Remediation

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


## Effective configuration source

The telemetry service does not automatically load `config/aipm.yaml` from the repository. `Application.create()` constructs `ConfigManager()` without an explicit path. `ConfigManager` first honors the `AIPM_CONFIG` environment variable; when that variable is absent, it resolves the current process user’s home directory and loads:

```text
~/.config/aipm/config.yaml
```

For the reported production service, the process user/environment resolves this to:

```text
/home/mina/.config/aipm/config.yaml
```

The repository file `config/aipm.yaml` is therefore a configuration example/documentation artifact unless the service explicitly sets `AIPM_CONFIG=/home/ubuntu/AIPM/config/aipm.yaml` or copies equivalent content into the user-level configuration path. A systemd service can also define an environment file or `Environment=` value that changes `AIPM_CONFIG`; the effective unit environment must be inspected before deployment.

There must be one documented production source of truth. The production configuration must not contain a whole-home discovery root. If `AIPM_CONFIG` is intentionally used, record its absolute path in the service deployment record and validate the loaded `discovery.search_paths` at startup. The configuration validator rejects any search path equal to the current process user’s entire home directory, so a broad `~` or `/home/<user>` root cannot silently reappear.

## Approved production discovery roots

The repository configuration example now uses only these explicit roots:

```text
/home/ubuntu/aipm
/home/ubuntu/fastsdcpu
/home/ubuntu/invoicing
/home/ubuntu/local-ai-packaged
/home/ubuntu/EAG
```

These paths are intentionally explicit. Missing roots are skipped as unavailable inputs; the service does not replace them with `/home/ubuntu` or another parent directory.

## Discovery and Git safety limits

Project discovery retains the existing depth and symlink behavior while adding bounds for total directories, total entries, projects, Git enrichments, Git command duration, and per-project Git result items. Invalid non-positive limits fail configuration validation. Telemetry discovery checks a cooperative cancellation event and monotonic deadline between filesystem operations and before each Git enrichment.

Telemetry uses a bounded Git snapshot path. Git commands are fixed, read-only commands launched with `shell=False`, `GIT_OPTIONAL_LOCKS=0`, a new process group, a hard deadline, and capped stdout/stderr. Untracked, modified, conflicted, stash, and ahead/behind results are bounded. Git timeout, output overflow, non-zero status, and cancellation return safe telemetry-unavailable behavior.

## Shutdown semantics

Python cannot forcibly cancel an arbitrary worker thread. The telemetry coordinator therefore provides cooperative cancellation: stop requests set the coordinator event and the active project slot’s cancellation event; project discovery and bounded Git operations check those signals. The existing single-flight rule remains in place, so a second project refresh is not started while one is active.

This application change reduces ongoing CPU and makes cooperative stop responsive when the active work honors its cancellation boundary. It does not claim that a thread blocked inside an uninterruptible external operation can be force-killed. A separate reviewed systemd change remains required for a hard process-level guarantee, including one canonical telemetry service instance, duplicate user/system unit resolution, an explicit `CPUQuota`, an aligned `TimeoutStopSec`, `KillMode=control-group` if child processes are used, and a bounded `TasksMax`. Those unit changes are outside this remediation phase.

## Scope

This remediation does not modify MC-6.12A or MC-6.12B, does not provision Gate 3, does not create `/opt/aipm-provenance`, does not install or modify systemd units, and does not change Docker, Cloudflare, databases, credentials, notifications, or public ingress.
