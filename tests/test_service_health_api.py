from datetime import datetime, timedelta, timezone

from aipm.capabilities.dashboard.service_health_api import DashboardServiceHealthApi


class FakeDashboardApi:
    def __init__(self, available=True, generated_at=None):
        self.available = available
        self.generated_at = generated_at or datetime.now(timezone.utc).isoformat()

    def overview(self):
        return {
            "generated_at": self.generated_at,
            "host": {"available": self.available},
        }


class FakeIncidentsApi:
    def __init__(self, payload):
        self.payload = payload

    def events(self, **_filters):
        return self.payload


def test_service_health_reports_fresh_and_never_sampled():
    api = DashboardServiceHealthApi(
        FakeDashboardApi(),
        FakeIncidentsApi({"available": True, "events": []}),
        stale_after_seconds=45,
    )

    response = api.services()

    assert response["available"] is True
    assert response["services"]["telemetry"]["state"] == "fresh"
    assert response["services"]["mc3"]["state"] == "never_sampled"
    assert response["overall"] == "healthy"


def test_service_health_reports_stale_and_unavailable_safely():
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    api = DashboardServiceHealthApi(
        FakeDashboardApi(generated_at=old),
        FakeIncidentsApi({"available": False, "error": "hidden internal error"}),
        stale_after_seconds=45,
    )

    response = api.services()

    assert response["services"]["telemetry"]["state"] == "stale"
    assert response["services"]["mc3"]["state"] == "unavailable"
    assert "hidden internal error" not in str(response)
    assert response["overall"] == "unavailable"


def test_mc3_selects_latest_event_from_ascending_results():
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    api = DashboardServiceHealthApi(
        FakeDashboardApi(),
        FakeIncidentsApi({"available": True, "events": [
            {"occurred_at": old},
            {"occurred_at": recent},
        ]}),
        stale_after_seconds=45,
    )

    mc3 = api.services()["services"]["mc3"]

    assert mc3["state"] == "fresh"
    assert mc3["last_observed_at"] == recent.replace(" ", "T") or mc3["last_observed_at"] == recent


def test_mc3_reports_fresh_even_when_latest_event_is_old():
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    api = DashboardServiceHealthApi(
        FakeDashboardApi(),
        FakeIncidentsApi({"available": True, "events": [{"occurred_at": old}]}),
        stale_after_seconds=45,
    )

    mc3 = api.services()["services"]["mc3"]

    assert mc3["state"] == "fresh"
    assert mc3["age_seconds"] >= 21600 - 5
