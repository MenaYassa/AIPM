"""Safety helpers for read-only Mission Control payload fixtures."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Iterable


_KEY_PATTERN = re.compile(r"(?:token|password|secret|api[_-]?key|webhook|authorization|credential|destination)", re.IGNORECASE)
_KEY_VALUE_PATTERN = re.compile(r"(?:BEGIN [A-Z ]+ KEY|https?://[^\s]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SafetyFinding:
    """Location and category of a value that must not reach a UI/API payload."""

    path: str
    category: str


def scan_payload(payload: Any, *, allow_loopback_urls: bool = True) -> tuple[SafetyFinding, ...]:
    """Return secret-like keys or unsafe values found in a JSON-like payload.

    This scanner is intentionally conservative for fixtures and contract tests.
    It does not inspect process environments, files, databases, or providers.
    """

    findings: list[SafetyFinding] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if _KEY_PATTERN.search(key_text):
                    findings.append(SafetyFinding(child_path, "secret-like-key"))
                visit(child, child_path)
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
            return
        if isinstance(value, str):
            for match in _KEY_VALUE_PATTERN.finditer(value):
                token = match.group(0)
                if token.lower().startswith("http") and allow_loopback_urls and _is_loopback_url(token):
                    continue
                category = "external-url" if token.lower().startswith("http") else "secret-like-value"
                findings.append(SafetyFinding(path or "$", category))

    visit(payload, "")
    return tuple(findings)


def assert_safe_payload(payload: Any, *, allow_loopback_urls: bool = True) -> None:
    """Raise a stable error if a fixture or response contains unsafe material."""

    findings = scan_payload(payload, allow_loopback_urls=allow_loopback_urls)
    if findings:
        detail = ", ".join(f"{item.path}:{item.category}" for item in findings)
        raise ValueError(f"unsafe Mission Control payload: {detail}")


def _is_loopback_url(value: str) -> bool:
    match = re.match(r"^https?://([^/:]+)(?::\d+)?(?:/|$)", value, re.IGNORECASE)
    if not match:
        return False
    host = match.group(1).strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
