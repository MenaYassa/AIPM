from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from aipm.engines.health.engine import HealthEngine
from aipm.engines.health.report_builder import ReportBuilder
from aipm.models.finding import Finding, Severity
from aipm.models.project import Project
from aipm.models.health import HealthState
from aipm.mappers.docker import DockerMapper


def test_report_builder_calculates_score_state_and_recommendations():
    findings = [
        Finding("critical", "Docker", Severity.CRITICAL, "Critical", "bad", "Fix critical"),
        Finding("warning", "Git", Severity.WARNING, "Warning", "bad", "Fix warning"),
        Finding("info", "Git", Severity.INFO, "Info", "note", "Fix warning"),
    ]

    report = ReportBuilder().build(Project(name="demo", path="/tmp/demo"), findings)

    assert report.score == 40
    assert report.state is HealthState.CRITICAL
    assert report.critical == 1
    assert report.warning == 1
    assert [recommendation.action for recommendation in report.recommendations] == ["Fix critical", "Fix warning"]


def test_health_engine_contains_failure_as_finding():
    class BrokenAnalyzer:
        def analyze(self, project):
            raise RuntimeError("fixture failure")

    report = HealthEngine(analyzers=[BrokenAnalyzer()]).analyze(Project(name="demo", path="/tmp/demo"))

    assert report.score == 90
    assert report.findings[0].code == "ANALYZER_FAILED"
    assert report.findings[0].component == "BrokenAnalyzer"


def test_docker_mapper_handles_missing_created_timestamp():
    container = SimpleNamespace(
        short_id="abc123",
        id="abc123",
        name="demo",
        image=SimpleNamespace(tags=[]),
        attrs={"State": {"Status": "running"}, "Config": {"Labels": {}}},
        labels={},
        ports={},
    )

    mapped = DockerMapper.container(container)

    assert mapped.name == "demo"
    assert mapped.state == "running"
    assert mapped.created == datetime.fromtimestamp(0, tz=timezone.utc)
