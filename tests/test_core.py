from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from git import Repo

from aipm.core.config import ConfigManager
from aipm.core.exceptions import AIPMError
import pytest

from aipm.models.config import DiscoveryConfig
from aipm.services.project.service import DiscoveryLimitExceeded
from aipm.models.project import Project
from aipm.services.backup.engine import BackupEngine
from aipm.services.project.service import ProjectService
from aipm.providers.git.provider import GitProvider


def test_config_manager_creates_and_loads_default(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    created = ConfigManager(config_path)
    loaded = ConfigManager(config_path)

    assert config_path.is_file()
    assert created.config.discovery.search_paths == loaded.config.discovery.search_paths
    assert loaded.config.logging.level == "INFO"


def test_project_discovery_honors_depth_and_returns_git_state(tmp_path: Path):
    compose_project = tmp_path / "compose-project"
    compose_project.mkdir()
    (compose_project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    git_project = tmp_path / "git-project"
    git_project.mkdir()
    repo = Repo.init(git_project)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "AIPM Test")
        writer.set_value("user", "email", "aipm@example.test")
    (git_project / "README.md").write_text("test\n", encoding="utf-8")
    (git_project / ".gitignore").write_text("nested/\n", encoding="utf-8")

    nested = git_project / "nested"
    nested.mkdir()
    (nested / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    repo.index.add(["README.md", ".gitignore"])
    repo.index.commit("initial")

    config = SimpleNamespace(
        discovery=DiscoveryConfig(search_paths=[str(tmp_path)], max_depth=3)
    )
    service = ProjectService(app=SimpleNamespace(config=config))
    projects = service.discover(bounded=True)

    names = {project.name for project in projects}
    assert names == {"compose-project", "git-project"}
    git_state = next(project.git for project in projects if project.name == "git-project")
    assert git_state is not None
    assert git_state.exists is True
    assert git_state.dirty is False
    assert git_state.last_commit_message == "initial"


def test_project_discovery_stops_at_directory_bound(tmp_path: Path):
    for index in range(4):
        (tmp_path / f"dir-{index}").mkdir()
    config = SimpleNamespace(discovery=DiscoveryConfig(search_paths=[str(tmp_path)], max_directories=2))
    service = ProjectService(app=SimpleNamespace(config=config))
    with pytest.raises(DiscoveryLimitExceeded, match="directory bound"):
        service.discover(bounded=True)


def test_project_discovery_stops_at_project_bound(tmp_path: Path):
    for index in range(3):
        project = tmp_path / f"project-{index}"
        project.mkdir()
        (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    config = SimpleNamespace(discovery=DiscoveryConfig(search_paths=[str(tmp_path)], max_projects=2))
    service = ProjectService(app=SimpleNamespace(config=config))
    with pytest.raises(DiscoveryLimitExceeded, match="project bound"):
        service.discover(bounded=True)


def test_project_discovery_rejects_whole_home_root(monkeypatch, tmp_path: Path):
    from aipm.core.config import ConfigManager
    monkeypatch.setattr("aipm.core.config.Path.home", lambda: tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(f"logging: {{}}\ndiscovery:\n  search_paths: ['{tmp_path}']\ntelemetry:\n  database_path: /tmp/mc.db\n", encoding="utf-8")
    with pytest.raises(AIPMError, match="entire home"):
        ConfigManager(path)


def test_backup_excludes_generated_directories(tmp_path: Path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "config.yaml").write_text("key: value\n", encoding="utf-8")
    (project_path / "node_modules").mkdir()
    (project_path / "node_modules" / "generated.js").write_text("generated\n", encoding="utf-8")

    archive = BackupEngine(tmp_path / "backups").create_snapshot(
        Project(name="project", path=str(project_path))
    )

    assert archive.archive_path.is_file()
    import tarfile

    with tarfile.open(archive.archive_path, "r:gz") as handle:
        names = handle.getnames()
    assert "project/config.yaml" in names
    assert not any("node_modules" in name for name in names)
