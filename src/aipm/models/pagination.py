from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_CURSOR_LENGTH = 256


def _epoch_seconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _validate_cursor(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_CURSOR_LENGTH or any(char.isspace() for char in value):
        raise CursorError("cursor is outside the supported bounds")
    return value


class CursorError(ValueError):
    """Raised when a bounded cursor is malformed, tampered, or mismatched."""


@dataclass(frozen=True, slots=True)
class KeysetCursor:
    family: str
    direction: str
    occurred_at: datetime
    item_id: int
    fingerprint: str
    version: int = 2
    start_at: datetime | None = None
    end_at: datetime | None = None

    _KEY = b"aipm-mission-control-keyset-cursor-v1"

    def encode(self) -> str:
        payload = {
            "v": self.version,
            "f": self.family,
            "d": self.direction,
            "a": _epoch_seconds(self.occurred_at),
            "i": self.item_id,
            "p": self.fingerprint,
            "s": _epoch_seconds(self.start_at) if self.start_at else None,
            "e": _epoch_seconds(self.end_at) if self.end_at else None,
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._KEY, body, hashlib.sha256).hexdigest()[:24].encode()
        encoded = base64.urlsafe_b64encode(body + b"." + signature).decode().rstrip("=")
        if len(encoded) > MAX_CURSOR_LENGTH:
            raise CursorError("cursor exceeds the supported bound")
        return encoded

    @classmethod
    def decode(cls, value: str) -> "KeysetCursor":
        try:
            checked = _validate_cursor(value)
            if checked is None:
                raise CursorError("cursor is required")
            padded = checked + "=" * (-len(checked) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode())
            body, signature = decoded.rsplit(b".", 1)
            expected = hmac.new(cls._KEY, body, hashlib.sha256).hexdigest()[:24].encode()
            if not hmac.compare_digest(signature, expected):
                raise CursorError("cursor signature is invalid")
            payload = json.loads(body.decode())
            family = payload["f"]
            direction = payload["d"]
            version = payload["v"]
            fingerprint = payload["p"]
            if version not in {1, 2} or not re.fullmatch(r"[a-z0-9-]{1,64}", family) or direction not in {"asc", "desc"}:
                raise CursorError("cursor metadata is invalid")
            raw_occurred_at = payload["a"]
            start_at = None
            end_at = None
            raw_start = payload.get("s")
            raw_end = payload.get("e")
            if version == 2:
                if not isinstance(raw_occurred_at, int) or isinstance(raw_occurred_at, bool):
                    raise CursorError("cursor position is invalid")
                if raw_start is None and raw_end is None:
                    occurred_at = datetime.fromtimestamp(raw_occurred_at, timezone.utc)
                elif isinstance(raw_start, int) and isinstance(raw_end, int) and not isinstance(raw_start, bool) and not isinstance(raw_end, bool) and raw_start <= raw_end:
                    occurred_at = datetime.fromtimestamp(raw_occurred_at, timezone.utc)
                    start_at = datetime.fromtimestamp(raw_start, timezone.utc)
                    end_at = datetime.fromtimestamp(raw_end, timezone.utc)
                else:
                    raise CursorError("cursor boundary is invalid")
            else:
                occurred_at = datetime.fromisoformat(raw_occurred_at)
                start_at = datetime.fromisoformat(raw_start) if raw_start is not None else None
                end_at = datetime.fromisoformat(raw_end) if raw_end is not None else None
            item_id = payload["i"]
            if occurred_at.tzinfo is None or (start_at is not None and start_at.tzinfo is None) or (end_at is not None and end_at.tzinfo is None) or not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 0:
                raise CursorError("cursor position is invalid")
            if (start_at is None) != (end_at is None) or (start_at is not None and end_at is not None and start_at > end_at):
                raise CursorError("cursor boundary is invalid")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{16,64}", fingerprint):
                raise CursorError("cursor binding is invalid")
            return cls(family, direction, occurred_at, item_id, fingerprint, version, start_at, end_at)
        except CursorError:
            raise
        except Exception as exc:
            raise CursorError("invalid cursor") from exc
