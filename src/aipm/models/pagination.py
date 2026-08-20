from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime

MAX_CURSOR_LENGTH = 256


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
    version: int = 1
    start_at: datetime | None = None
    end_at: datetime | None = None

    _KEY = b"aipm-mission-control-keyset-cursor-v1"

    def encode(self) -> str:
        payload = {
            "v": self.version,
            "f": self.family,
            "d": self.direction,
            "a": self.occurred_at.isoformat(),
            "i": self.item_id,
            "p": self.fingerprint,
            "s": self.start_at.isoformat() if self.start_at else None,
            "e": self.end_at.isoformat() if self.end_at else None,
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
            occurred_at = datetime.fromisoformat(payload["a"])
            item_id = payload["i"]
            fingerprint = payload["p"]
            version = payload["v"]
            start_at = datetime.fromisoformat(payload["s"]) if payload.get("s") else None
            end_at = datetime.fromisoformat(payload["e"]) if payload.get("e") else None
            if version != 1 or not re.fullmatch(r"[a-z0-9-]{1,64}", family) or direction not in {"asc", "desc"}:
                raise CursorError("cursor metadata is invalid")
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
