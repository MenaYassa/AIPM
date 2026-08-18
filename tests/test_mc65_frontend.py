from __future__ import annotations

from pathlib import Path


HTML = Path(__file__).parents[1] / "src" / "aipm" / "dashboard" / "static" / "index.html"


def test_docker_view_contains_detail_and_inventory_sections() -> None:
    text = HTML.read_text(encoding="utf-8")
    for marker in (
        'data-view="docker"',
        "Container operations",
        'id="dockerGroups"',
        'id="dockerDetailContainers"',
        'id="dockerImages"',
        'id="dockerVolumes"',
        'id="dockerNetworks"',
        "read-only detail",
        "/api/docker/summary?limit=200",
        "/api/docker/${key}?limit=200",
    ):
        assert marker in text


def test_docker_page_preserves_scheduler_and_read_only_ui_contract() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert "scheduler.register('docker',loadDocker,{intervalMs:30000})" in text
    assert "scheduler.register('docker-inventory',loadDockerInventory,{intervalMs:60000})" in text
    assert 'method="post"' not in text
    assert 'method="put"' not in text
    assert 'method="patch"' not in text
    assert 'method="delete"' not in text
    assert "/docker/start" not in text
    assert "/docker/stop" not in text
    assert "/docker/restart" not in text
    assert "/docker/exec" not in text
    assert "process.env" not in text
    assert "PRIVATE_TOKEN" not in text


def test_docker_view_reuses_existing_shell_static_modules() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert 'href="#/docker"' in text
    assert '/static/mission-control-state.js' in text
    assert '/static/mission-control-scheduler.js' in text
    assert '/static/mission-control-shell.js' in text
