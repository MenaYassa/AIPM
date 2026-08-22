from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.models.config import TelemetryConfig
from aipm.providers.git.provider import GitError, GitProvider
from aipm.services.telemetry.coordinator import TelemetrySamplingCoordinator, _SingleFlightSlot


def _fake_git(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "git"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_bounded_git_timeout_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_git(tmp_path, "sleep 2")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    started = time.monotonic()
    with pytest.raises(GitError, match="timeout"):
        GitProvider._run_bounded_git(str(tmp_path), ("status",), timeout_seconds=0.05, output_limit=1024)
    assert time.monotonic() - started < 1.0


def test_bounded_git_output_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_git(tmp_path, "printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n'")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(GitError, match="output bound"):
        GitProvider._run_bounded_git(str(tmp_path), ("status",), timeout_seconds=1, output_limit=16)


def test_project_slot_cancellation_is_cooperative() -> None:
    cancelled = False
    slot = _SingleFlightSlot("project")

    def work(cancel_event, _deadline):
        nonlocal cancelled
        while not cancel_event.is_set():
            time.sleep(0.001)
        cancelled = True

    assert slot.start(work, timeout_seconds=1, cancellable=True)
    time.sleep(0.02)
    slot.cancel()
    deadline = time.monotonic() + 1
    while slot.state().running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cancelled is True
    assert slot.state().running is False


def test_coordinator_stop_does_not_wait_for_project_work() -> None:
    finished = False

    class Sampler:
        def refresh_project_once(self, cancel_event, _deadline):
            nonlocal finished
            while not cancel_event.is_set():
                time.sleep(0.001)
            finished = True

        def sample_fast_once(self):
            return SimpleNamespace(error=None)

        def refresh_resource_once(self):
            return None

    config = TelemetryConfig(interval_seconds=0.01, project_interval_seconds=0.01, resource_sampling_enabled=False, project_timeout_seconds=5)
    coordinator = TelemetrySamplingCoordinator(Sampler(), config)
    runner = __import__("threading").Thread(target=coordinator.run, daemon=True)
    runner.start()
    time.sleep(0.05)
    started = time.monotonic()
    coordinator.request_stop()
    runner.join(timeout=1)
    assert not runner.is_alive()
    assert time.monotonic() - started < 0.5
    deadline = time.monotonic() + 1
    while not finished and time.monotonic() < deadline:
        time.sleep(0.005)
    assert finished is True
