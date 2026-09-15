import pytest
from unittest.mock import MagicMock
from aipm.models.git import GitRepository
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdateRisk
from aipm.providers.systemd_observation import SystemdObservationProvider, SystemdUnitObservation
from aipm.services.project.service import ProjectService
from aipm.services.update.planner import UpdatePlanner


def test_planner_precedence_start_services(tmp_path):
    (tmp_path / "start_services.py").write_text("print('ok')\n")
    project = Project(
        name="test-proj",
        path=str(tmp_path),
        capabilities=ProjectCapabilities(has_compose=True, has_systemd=True, systemd_units=["test.service"]),
    )
    mock_ps = MagicMock(spec=ProjectService)
    mock_ps.get_project.return_value = project

    planner = UpdatePlanner(project_service=mock_ps)
    plan = planner.plan("test-proj", dry_run=True)
    assert plan.runtime_mode == "custom"
    assert "Run the project start_services.py orchestration script" in plan.actions


def test_planner_precedence_compose(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("version: '3'\nservices:\n  app:\n    image: alpine\n")
    project = Project(
        name="test-proj",
        path=str(tmp_path),
        capabilities=ProjectCapabilities(has_compose=True, has_systemd=True, systemd_units=["test.service"]),
        compose_files=[str(tmp_path / "docker-compose.yml")],
    )
    mock_ps = MagicMock(spec=ProjectService)
    mock_ps.get_project.return_value = project

    planner = UpdatePlanner(project_service=mock_ps)
    plan = planner.plan("test-proj", dry_run=True)
    assert plan.runtime_mode == "compose"
    assert any("Rebuild and start the project Compose services" in a for a in plan.actions)


def test_planner_systemd_success(tmp_path):
    project = Project(
        name="test-proj",
        path=str(tmp_path),
        capabilities=ProjectCapabilities(has_systemd=True, systemd_units=["test.service"]),
        git=GitRepository(exists=True, branch="main"),
    )
    mock_ps = MagicMock()
    mock_ps.get_project.return_value = project
    mock_ps.app.config.projects = {}

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    obs = SystemdUnitObservation(
        unit_name="test.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory=str(tmp_path),
        exec_start=None,
        user="ubuntu",
        fragment_path="/etc/systemd/system/test.service",
        primary_id="test.service",
        invocation_id="12345",
        active_enter_timestamp="1000",
        restart_count=0,
    )
    mock_obs_provider.validate_trust.return_value = (True, None, obs)

    planner = UpdatePlanner(project_service=mock_ps, systemd_provider=mock_obs_provider)
    plan = planner.plan("test-proj", dry_run=True)
    assert plan.proceed is True
    assert plan.runtime_mode == "systemd"
    assert plan.systemd_units == ["test.service"]
    assert plan.systemd_action == "try-restart"
    assert "Restart and verify systemd service(s) (test.service)" in plan.actions
    assert "Verify health after the update" in plan.actions


def test_planner_systemd_inactive_unit_fails(tmp_path):
    project = Project(
        name="test-proj",
        path=str(tmp_path),
        capabilities=ProjectCapabilities(has_systemd=True, systemd_units=["test.service"]),
        git=GitRepository(exists=True, branch="main"),
    )
    mock_ps = MagicMock()
    mock_ps.get_project.return_value = project
    mock_ps.app.config.projects = {}

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    obs = SystemdUnitObservation(
        unit_name="test.service",
        load_state="loaded",
        active_state="inactive",
        sub_state="dead",
        unit_file_state="enabled",
        working_directory=str(tmp_path),
        exec_start=None,
        user="ubuntu",
        fragment_path="/etc/systemd/system/test.service",
        primary_id="test.service",
        invocation_id=None,
        active_enter_timestamp=None,
        restart_count=0,
    )
    mock_obs_provider.validate_trust.return_value = (True, None, obs)

    planner = UpdatePlanner(project_service=mock_ps, systemd_provider=mock_obs_provider)
    plan = planner.plan("test-proj", dry_run=True)
    assert plan.proceed is False
    assert plan.risk == UpdateRisk.BLOCKED
    assert any("try-restart requires an active unit" in r for r in plan.reasons)
