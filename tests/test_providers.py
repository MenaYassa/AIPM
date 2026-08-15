from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.models.project import Project
from aipm.providers.compose import provider as compose_module
from aipm.providers.compose.provider import ComposeError, ComposeProvider


def test_compose_command_uses_all_project_files(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(compose_module.shutil, "which", lambda name: "/usr/bin/docker")
    project = Project(
        name="demo",
        path=str(tmp_path),
        compose_files=[str(tmp_path / "compose.yaml"), str(tmp_path / "override.yaml")],
    )

    command = ComposeProvider()._compose_command(project, "config")

    assert command == [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "compose.yaml"),
        "-f",
        str(tmp_path / "override.yaml"),
        "config",
    ]


def test_compose_command_reports_missing_docker(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(compose_module.shutil, "which", lambda name: None)
    project = Project(name="demo", path=str(tmp_path), compose_files=[str(tmp_path / "compose.yaml")])

    with pytest.raises(ComposeError, match="Docker CLI"):
        ComposeProvider()._compose_command(project, "up")


def test_compose_run_translates_command_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(compose_module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        compose_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad compose file"),
    )
    project = Project(name="demo", path=str(tmp_path), compose_files=[str(tmp_path / "compose.yaml")])

    with pytest.raises(ComposeError, match="bad compose file"):
        ComposeProvider().up(project)
