"""CI workflow contract tests.

These tests certify the posture of .github/workflows/ci.yml so the
workflow cannot silently drift from the validation-only contract the
repository documents (CONTRIBUTING.md, SECURITY.md, and the dated
reconciliations):

1. The workflow YAML parses and declares exactly the expected
   validation job structure.
2. Triggers are push-to-main and pull_request only — never
   pull_request_target (which grants pull-request code access to
   repository-scoped secrets).
3. Permissions are explicitly contents: read and nothing else.
4. Every `uses:` step pins an action to a full 40-hex commit SHA, not
   a mutable tag or branch.
5. No step references secrets, and run steps contain no
   publish/deploy/network-egress commands (validation only).
6. The expected validation chain is present in order: locked install,
   full pytest with no exclusions, development validation, release
   build, production validation.
7. No continue-on-error anywhere: validation failures fail the build.

The tests read the workflow as a static YAML document; they never
execute workflow code and never contact GitHub.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Full-length commit-SHA pins, kept in step with ci.yml. Changing an
# action pin requires updating both files deliberately.
PINNED_ACTIONS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
}

# Commands that must never appear in a validation-only workflow's run
# steps: publishing, deployment, remote mutation, and network egress.
FORBIDDEN_RUN_COMMANDS = (
    "git push",
    "git remote set-url",
    "gh ",
    "docker ",
    "docker-compose",
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    "twine",
    "publish",
    "pip install",
    "uv publish",
)

COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML parses the bare YAML 1.1 `on:` key as boolean True.
    return workflow.get("on", workflow.get(True, {}))


def _steps(job: dict) -> list[dict]:
    return job["steps"]


def _run_steps(steps: list[dict]) -> list[str]:
    return [s["run"] for s in steps if "run" in s]


class TestWorkflowParses:
    def test_workflow_file_exists_and_parses(self):
        assert WORKFLOW.is_file()
        workflow = _load_workflow()
        assert isinstance(workflow, dict)

    def test_declares_exactly_one_validation_job(self):
        workflow = _load_workflow()
        assert set(workflow["jobs"]) == {"validate"}
        assert workflow["jobs"]["validate"]["runs-on"] == "ubuntu-latest"

    def test_timeout_is_bounded(self):
        workflow = _load_workflow()
        assert workflow["jobs"]["validate"]["timeout-minutes"] == 30


class TestTriggersAndPermissions:
    def test_triggers_are_push_to_main_and_pull_request(self):
        triggers = _triggers(_load_workflow())
        assert triggers["push"] == {"branches": ["main"]}
        # Bare `pull_request:` (null value) is valid and means default
        # pull-request behavior; assert the trigger key is present.
        assert "pull_request" in triggers

    def test_pull_request_target_is_never_used(self):
        triggers = _triggers(_load_workflow())
        assert "pull_request_target" not in triggers

    def test_permissions_are_read_only_contents(self):
        workflow = _load_workflow()
        assert workflow["permissions"] == {"contents": "read"}


class TestActionPinning:
    def test_every_uses_step_is_pinned_to_a_commit_sha(self):
        workflow = _load_workflow()
        for step in _steps(workflow["jobs"]["validate"]):
            if "uses" in step:
                ref = step["uses"].split("@", 1)[1]
                assert COMMIT_SHA_RE.match(ref), (
                    f"action ref must be a 40-hex commit SHA, got: {ref}"
                )

    def test_pinned_actions_and_versions_are_the_expected_ones(self):
        workflow = _load_workflow()
        used = {}
        for step in _steps(workflow["jobs"]["validate"]):
            if "uses" in step:
                action, ref = step["uses"].split("@", 1)
                used[action] = ref
        assert used == PINNED_ACTIONS

    def test_no_third_party_actions_beyond_the_documented_set(self):
        workflow = _load_workflow()
        for step in _steps(workflow["jobs"]["validate"]):
            if "uses" in step:
                action = step["uses"].split("@", 1)[0]
                assert action in PINNED_ACTIONS, action

    def test_checkout_does_not_persist_credentials(self):
        workflow = _load_workflow()
        for step in _steps(workflow["jobs"]["validate"]):
            if "uses" in step and step["uses"].startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


class TestValidationOnly:
    def test_no_step_references_secrets(self):
        workflow = _load_workflow()
        for step in _steps(workflow["jobs"]["validate"]):
            rendered = repr(step)
            assert "secrets." not in rendered

    def test_run_steps_contain_no_publish_deploy_or_egress_commands(self):
        workflow = _load_workflow()
        for run in _run_steps(_steps(workflow["jobs"]["validate"])):
            for forbidden in FORBIDDEN_RUN_COMMANDS:
                assert forbidden not in run, (
                    f"forbidden command {forbidden!r} in run step: {run}"
                )

    def test_no_continue_on_error(self):
        workflow = _load_workflow()
        for step in _steps(workflow["jobs"]["validate"]):
            assert "continue-on-error" not in step


class TestValidationChain:
    def test_expected_steps_present_in_order(self):
        workflow = _load_workflow()
        runs = _run_steps(_steps(workflow["jobs"]["validate"]))
        expected_in_order = [
            "uv sync --frozen --extra dev",
            ".venv/bin/python -m pytest -p no:cacheprovider -q",
            "uv run --no-sync ruff check .",
            ".venv/bin/python ops/validate-release.py --development",
            ".venv/bin/python ops/build-release.py",
            ".venv/bin/python ops/validate-release.py",
        ]
        positions = []
        for expected in expected_in_order:
            assert expected in runs, f"missing run step: {expected}"
            positions.append(runs.index(expected))
        assert positions == sorted(positions), (
            "validation chain steps are out of order"
        )

    def test_static_check_runs_after_tests_and_before_release_validation(self):
        workflow = _load_workflow()
        runs = _run_steps(_steps(workflow["jobs"]["validate"]))
        static_positions = [
            i for i, run in enumerate(runs) if "ruff check" in run
        ]
        assert static_positions, "static check step missing"
        assert len(static_positions) == 1, "exactly one static check step expected"
        static_pos = static_positions[0]
        pytest_positions = [
            i for i, run in enumerate(runs) if "pytest" in run
        ]
        release_positions = [
            i for i, run in enumerate(runs)
            if "ops/validate-release.py" in run or "ops/build-release.py" in run
        ]
        assert static_positions[0] > max(pytest_positions), (
            "static check must run after the test suite"
        )
        assert static_positions[0] < min(release_positions), (
            "static check must run before release validation"
        )

    def test_static_check_is_offline_locked_and_local(self):
        workflow = _load_workflow()
        runs = _run_steps(_steps(workflow["jobs"]["validate"]))
        static_runs = [run for run in runs if "ruff" in run]
        assert len(static_runs) == 1
        run = static_runs[0]
        # Uses the locked environment (--no-sync: no re-resolution, no
        # new network egress beyond the locked sync) and checks the
        # whole tree; no pip/wget/curl download path, no action.
        assert "ruff check ." in run
        assert "--no-sync" in run
        for token in ("pip install", "curl", "wget", "http"):
            assert token not in run, token

    def test_full_suite_runs_with_no_exclusions_or_deselects(self):
        workflow = _load_workflow()
        pytest_steps = [
            run
            for run in _run_steps(_steps(workflow["jobs"]["validate"]))
            if "pytest" in run
        ]
        assert len(pytest_steps) == 1
        for token in ("--deselect", "--ignore", "-k ", "--skip"):
            assert token not in pytest_steps[0], token

    def test_release_validation_uses_the_existing_tooling(self):
        workflow = _load_workflow()
        runs = _run_steps(_steps(workflow["jobs"]["validate"]))
        assert any("ops/validate-release.py --development" in r for r in runs)
        assert any(r.strip() == ".venv/bin/python ops/validate-release.py" for r in runs)
        assert any("ops/build-release.py" in r for r in runs)
