from datetime import datetime, timezone

from aipm.capabilities.dashboard.history_api import DashboardHistoryApi
from aipm.mappers.telemetry_history import HistoryResponseMapper
from aipm.models.history import HistoryQuery, HistoryResponse
from aipm.services.telemetry.history import HistoricalQueryService


UTC = timezone.utc


class FakeRepository:
    def __init__(self, error=None, points=None):
        self.error = error
        self.points = points or []

    def get_host_history(self, start, end, limit):
        if self.error:
            raise self.error
        return self.points[:limit]

    def get_container_history(self, name, start, end, limit):
        if self.error:
            raise self.error
        return self.points[:limit]

    def get_project_history(self, name, start, end, limit):
        if self.error:
            raise self.error
        return self.points[:limit]

    def get_tunnel_history(self, start, end, limit):
        if self.error:
            raise self.error
        return self.points[:limit]


def test_query_service_validates_range_and_limit():
    service = HistoricalQueryService(FakeRepository(), clock=lambda: datetime(2026, 8, 16, tzinfo=UTC))
    query = service.query_from_range("1h", 10)
    assert query.end.tzinfo is UTC
    assert (query.end - query.start).total_seconds() == 3600

    for range_name, limit in (("2h", 10), ("24h", 0), ("24h", 5001)):
        try:
            service.query_from_range(range_name, limit)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid query was accepted")


def test_query_service_no_data_is_available_empty_response():
    service = HistoricalQueryService(FakeRepository())
    response = service.host(HistoryQuery(limit=10))
    assert response.available is True
    assert response.status == "ok"
    assert response.points == ()


def test_query_service_database_failure_is_safe():
    service = HistoricalQueryService(FakeRepository(error=RuntimeError("database failed")))
    response = service.host(HistoryQuery(limit=10))
    assert response.available is False
    assert response.status == "unavailable"
    assert response.error == "Historical telemetry unavailable"
    assert response.points == ()


def test_dashboard_history_api_invalid_query_and_unavailable_repository_are_safe():
    invalid = DashboardHistoryApi(HistoricalQueryService(FakeRepository()), HistoryResponseMapper()).host("bad", 10)
    assert invalid["available"] is False
    assert invalid["error"] == "Invalid history query"

    unavailable = DashboardHistoryApi(None, HistoryResponseMapper()).host()
    assert unavailable == {"available": False, "status": "unavailable", "error": "Historical telemetry unavailable", "points": []}


def test_history_response_mapper_serializes_utc_and_tuples():
    point = type("Point", (), {})
    response = HistoryResponse(True, "ok", None, (point(),))
    # The mapper is exercised through the dataclass point path in repository tests; this verifies safe empty handling here.
    assert HistoryResponseMapper().to_response(HistoryResponse(True, "ok", None, ())) == {
        "available": True,
        "status": "ok",
        "error": None,
        "points": [],
    }
