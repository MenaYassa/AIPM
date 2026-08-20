from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True, slots=True)
class TuiProjection:
    title: str
    status: str
    available: bool
    lines: tuple[tuple[str, str], ...] = ()
    records: tuple[tuple[str, str], ...] = ()
    next_cursor: str | None = None
    has_more: bool = False


_VIEW_LABELS = {
    "overview": "Overview",
    "server": "Server",
    "docker": "Docker",
    "projects": "Projects",
    "systemd": "Systemd",
    "logs": "Logs",
    "events": "Events",
    "incidents": "Incidents",
    "timeline": "Incident timeline",
    "history": "History",
    "settings": "Settings & Notification Posture",
}


def render_projection(console: Console, projection: TuiProjection) -> None:
    """Render a safe, bounded projection through Rich."""

    state = projection.status or ("ok" if projection.available else "unavailable")
    state_style = "green" if projection.available else "yellow"
    header = _literal_text(f"{projection.title} · {state}")
    header.stylize(state_style)
    parts: list[Any] = [header]
    if projection.lines:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value", overflow="fold")
        for label, value in projection.lines[:24]:
            table.add_row(_literal_text(_clip(label)), _literal_text(_clip(value)))
        parts.append(table)
    if projection.records:
        table = Table(show_header=True, box=None, expand=True)
        table.add_column("Item", style="cyan", no_wrap=True)
        table.add_column("State", overflow="fold")
        for item, value in projection.records[:40]:
            table.add_row(_literal_text(_clip(item)), _literal_text(_clip(value)))
        parts.append(table)
    if projection.has_more or projection.next_cursor:
        parts.append(Text("More bounded data is available; use n/next to continue.", style="dim"))
    console.print(Panel(Group(*parts), title=projection.title, border_style=state_style))


def project_view(view: str, response: Mapping[str, Any]) -> TuiProjection:
    """Convert an allow-listed façade response into a typed terminal projection."""

    title = _VIEW_LABELS.get(view, "Mission Control")
    status = _safe_text(response.get("status"), "unknown")
    available = _infer_available(view, response, status)
    error = response.get("error")
    lines: list[tuple[str, str]] = [("Observation", status)]
    if error:
        lines.append(("Message", _safe_text(error)))
    records: list[tuple[str, str]] = []
    next_cursor = _opaque_cursor(response.get("next_cursor"))
    has_more = bool(response.get("has_more", False))

    if view == "settings":
        return _project_settings(response, title, available=available, status=status)
    if view == "logs":
        entries = response.get("entries")
        sources = response.get("sources")
        lines.extend(
            [
                ("Entries", str(len(entries)) if isinstance(entries, list) else "—"),
                ("Sources", str(len(sources)) if isinstance(sources, list) else "—"),
                ("Truncated", _safe_text(response.get("truncated"), "unknown")),
            ]
        )
    elif view in {"events", "incidents", "timeline"}:
        key = "events" if view == "events" else "incidents" if view == "incidents" else "timeline"
        items = response.get(key)
        lines.append(("Items", str(len(items)) if isinstance(items, list) else "—"))
        records = _record_states(items)
    elif view == "history":
        points = response.get("points")
        changes = response.get("changes")
        lines.extend(
            [
                ("Points", str(len(points)) if isinstance(points, list) else "—"),
                ("Changes", str(len(changes)) if isinstance(changes, list) else "—"),
                ("Range", _safe_text(response.get("range"), "bounded")),
            ]
        )
    elif view == "projects":
        items = response.get("projects") or response.get("applications")
        lines.append(("Items", str(len(items)) if isinstance(items, list) else "—"))
        records = _record_states(items)
    elif view == "systemd":
        items = response.get("units")
        lines.append(("Allow-listed units", str(len(items)) if isinstance(items, list) else "—"))
        records = _record_states(items, name_keys=("id", "unit_id", "name"))
    elif view == "docker":
        items = response.get("containers")
        lines.append(("Containers", str(len(items)) if isinstance(items, list) else "—"))
        records = _record_states(items, name_keys=("name", "id", "container_id"))
    elif view == "server":
        _append_safe_section(lines, response, "host", "Host")
        _append_safe_section(lines, response, "health", "Health")
    elif view == "overview":
        _append_safe_section(lines, response, "snapshot", "Snapshot")
        _append_safe_section(lines, response, "services", "Services")
    else:
        lines.append(("Result", "No observation selected"))

    return TuiProjection(
        title=title,
        status=status,
        available=available,
        lines=tuple(lines),
        records=tuple(records),
        next_cursor=next_cursor,
        has_more=has_more,
    )


def _project_settings(response: Mapping[str, Any], title: str, *, available: bool, status: str) -> TuiProjection:
    notifications = response.get("notifications")
    audit = notifications.get("audit") if isinstance(notifications, Mapping) else None
    database = response.get("database") or response.get("read_only")
    lines = [("Observation", _safe_text(response.get("status"), "ok"))]
    if isinstance(response.get("deployment"), Mapping):
        deployment = response["deployment"]
        lines.extend(
            [
                ("Binding", _safe_text(deployment.get("binding"))),
                ("Public ingress", _safe_text(deployment.get("public_ingress"))),
                ("Permanent service", _safe_text(deployment.get("permanent_service"))),
            ]
        )
    if isinstance(database, Mapping):
        lines.append(("SQLite", _safe_text(database.get("sqlite_mode"))))
        lines.append(("Query-only", _safe_text(database.get("query_only"))))
    if isinstance(notifications, Mapping):
        lines.append(("Notifications", "enabled" if notifications.get("enabled") else "disabled"))
        lines.append(("Provider", _safe_text(notifications.get("provider_state"))))
    if isinstance(audit, Mapping):
        observed = audit.get("availability") == "observed"
        lines.append(("Audit status", "Observed" if observed else "Unavailable"))
        for key in ("pending", "sending", "sent", "failed", "unknown", "suppressed"):
            lines.append((key.replace("_", " ").title(), _safe_text(audit.get(key), "—")))
    return TuiProjection(
        title=title,
        status=status,
        available=available,
        lines=tuple(lines[:24]),
    )


def _infer_available(view: str, response: Mapping[str, Any], status: str) -> bool:
    explicit = response.get("available")
    if isinstance(explicit, bool):
        return explicit
    if view == "overview" and "available" not in response and "status" not in response:
        host = response.get("host")
        if isinstance(host, Mapping) and isinstance(host.get("available"), bool):
            return host["available"]
    if status in {"unavailable", "unknown", "not_observed", "error", "indeterminate"}:
        return False
    if status in {"fresh", "stale", "never_sampled", "ok", "healthy", "observed"}:
        return True
    return False


def _record_states(items: Any, *, name_keys: tuple[str, ...] = ("name", "id", "identifier")) -> list[tuple[str, str]]:
    if not isinstance(items, list):
        return []
    result: list[tuple[str, str]] = []
    for item in items[:40]:
        if not isinstance(item, Mapping):
            continue
        name = next((item.get(key) for key in name_keys if item.get(key) is not None), "item")
        state = item.get("state") or item.get("status") or item.get("observation") or "unknown"
        result.append((_safe_text(name), _safe_text(state)))
    return result


def _append_safe_section(lines: list[tuple[str, str]], response: Mapping[str, Any], key: str, label: str) -> None:
    value = response.get(key)
    if isinstance(value, Mapping):
        state = value.get("state") or value.get("status") or value.get("available")
        lines.append((label, _safe_text(state, "unknown")))


def _opaque_cursor(value: Any) -> str | None:
    """Return only an already-valid string cursor, byte-for-byte unchanged."""

    if isinstance(value, str) and value:
        return value
    return None


def _literal_text(value: str) -> Text:
    """Create literal terminal text with control bytes made visibly inert."""

    safe_chars: list[str] = []
    for char in value:
        codepoint = ord(char)
        if char in {"\n", "\t"}:
            safe_chars.append(char)
        elif codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            safe_chars.append(f"\\x{codepoint:02x}")
        else:
            safe_chars.append(char)
    return Text("".join(safe_chars))


def _safe_text(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:256]
    return fallback


def _clip(value: str) -> str:
    return value[:256]
