import pytest
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.update.plan_identity import UpdatePlanIdentity


def test_systemd_plan_identity_digest_fields():
    base_plan = UpdatePlan(
        project="aipm",
        project_path="/home/ubuntu/aipm",
        dry_run=False,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.MEDIUM,
        reasons=["Plan reason 1"],
        actions=["Action 1"],
        snapshot_required=True,
        estimated_restart=True,
        stash_required=False,
        pull_required=False,
        runtime_mode="systemd",
        systemd_units=["aipm-dashboard.service"],
        systemd_action="try-restart",
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
    )
    identity = UpdatePlanIdentity.from_plan(base_plan)
    digest = identity.digest()
    payload = identity.canonical_payload()

    assert payload["runtime_mode"] == "systemd"
    assert payload["systemd_units"] == ["aipm-dashboard.service"]
    assert payload["systemd_action"] == "try-restart"
    assert payload["health_probe_contract"] == "http:http://127.0.0.1:8787/healthz"

    # Mutating unit changes digest
    mutated_unit_plan = UpdatePlan(
        project="aipm",
        project_path="/home/ubuntu/aipm",
        dry_run=False,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.MEDIUM,
        reasons=["Plan reason 1"],
        actions=["Action 1"],
        snapshot_required=True,
        estimated_restart=True,
        stash_required=False,
        pull_required=False,
        runtime_mode="systemd",
        systemd_units=["other-dashboard.service"],
        systemd_action="try-restart",
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
    )
    assert UpdatePlanIdentity.from_plan(mutated_unit_plan).digest() != digest

    # Mutating action changes digest
    mutated_action_plan = UpdatePlan(
        project="aipm",
        project_path="/home/ubuntu/aipm",
        dry_run=False,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.MEDIUM,
        reasons=["Plan reason 1"],
        actions=["Action 1"],
        snapshot_required=True,
        estimated_restart=True,
        stash_required=False,
        pull_required=False,
        runtime_mode="systemd",
        systemd_units=["aipm-dashboard.service"],
        systemd_action="restart",
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
    )
    assert UpdatePlanIdentity.from_plan(mutated_action_plan).digest() != digest


def test_systemd_plan_identity_backward_compatibility():
    """Non-systemd plan without systemd fields produces identical keys as legacy."""
    plan = UpdatePlan(
        project="legacy-proj",
        project_path="/home/ubuntu/legacy",
        dry_run=False,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.LOW,
        reasons=[],
        actions=[],
        snapshot_required=True,
        estimated_restart=False,
        stash_required=False,
        pull_required=False,
        runtime_mode=None,
        systemd_units=[],
        systemd_action=None,
        health_probe_contract=None,
    )
    payload = UpdatePlanIdentity.from_plan(plan).canonical_payload()
    assert "runtime_mode" not in payload
    assert "systemd_units" not in payload
    assert "systemd_action" not in payload
    assert "health_probe_contract" not in payload
