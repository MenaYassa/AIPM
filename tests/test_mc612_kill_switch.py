from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.kill_switch import KillSwitchError, KillSwitchRegistry, KillSwitchState
from aipm.control_plane.project_plan import Environment


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_kill_switch_is_fail_closed_by_default():
    registry = KillSwitchRegistry(clock=lambda: NOW)
    assert registry.permits(Environment.STAGING) is False
    assert registry.permits(Environment.PRODUCTION) is False
    assert registry.switch(Environment.STAGING).state is KillSwitchState.ENGAGED
    assert registry.switch(Environment.PRODUCTION).state is KillSwitchState.PERMANENT


def test_staging_can_disengage_and_reengage_but_production_cannot():
    registry = KillSwitchRegistry(clock=lambda: NOW)
    later = NOW + timedelta(minutes=5)
    disengaged = registry.disengage(Environment.STAGING, reason="staging test enabled", now=later)
    assert disengaged.state is KillSwitchState.DISENGAGED
    assert registry.permits(Environment.STAGING) is True
    reengaged = registry.engage(Environment.STAGING, reason="opening complete", now=NOW + timedelta(minutes=10))
    assert reengaged.state is KillSwitchState.ENGAGED
    assert registry.permits(Environment.STAGING) is False
    with pytest.raises(KillSwitchError, match="permanently"):
        registry.disengage(Environment.PRODUCTION, now=later)


def test_production_remains_denied_even_if_allocator_would_disengage():
    registry = KillSwitchRegistry(clock=lambda: NOW)
    registry.engage_all(reason="emergency", now=NOW)
    assert registry.permits(Environment.PRODUCTION) is False
    assert registry.permits(Environment.STAGING) is False


def test_registry_configuration_is_immutable():
    registry = KillSwitchRegistry(clock=lambda: NOW)
    with pytest.raises(AttributeError):
        registry._switches = {}