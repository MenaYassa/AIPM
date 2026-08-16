from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aipm.models.notifications import DeliveryResult, DeliveryStatus, Notification, NotificationChannel


@dataclass(frozen=True, slots=True)
class DeliveryContext:
    provider_request_key: str
    timeout_seconds: int
    secret: str | None
    destination: str | None


class ChannelAdapter(Protocol):
    channel_type: str

    def send(self, notification: Notification, context: DeliveryContext) -> DeliveryResult: ...


class UnconfiguredAdapter:
    def __init__(self, channel_type: str):
        self.channel_type = channel_type

    def send(self, notification: Notification, context: DeliveryContext) -> DeliveryResult:
        return DeliveryResult(DeliveryStatus.FAILED, False, error_code="adapter_not_configured", error_message="No adapter is configured for this channel type")


class HttpAdapter:
    def __init__(self, channel_type: str = "http"):
        self.channel_type = channel_type

    def send(self, notification: Notification, context: DeliveryContext) -> DeliveryResult:
        if not context.destination:
            return DeliveryResult(DeliveryStatus.FAILED, False, error_code="destination_missing", error_message="Notification destination is not configured")
        payload = json.dumps({"title": notification.title, "body": notification.body, "incident_id": notification.incident_id, "severity": notification.severity.value, "request_key": context.provider_request_key}).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": context.provider_request_key}
        if context.secret:
            headers["Authorization"] = f"Bearer {context.secret}"
        try:
            with urlopen(Request(context.destination, data=payload, headers=headers, method="POST"), timeout=context.timeout_seconds) as response:
                status = int(response.status)
                return DeliveryResult(DeliveryStatus.SENT if 200 <= status < 300 else DeliveryStatus.FAILED, 500 <= status < 600, provider_status_code=status)
        except HTTPError as exc:
            return DeliveryResult(DeliveryStatus.FAILED, 500 <= exc.code < 600, provider_status_code=exc.code, error_code="http_error", error_message=f"HTTP status {exc.code}")
        except (URLError, TimeoutError):
            return DeliveryResult(DeliveryStatus.UNKNOWN, False, error_code="network_unknown", error_message="Network outcome was ambiguous")


class TelegramAdapter(HttpAdapter):
    def __init__(self):
        super().__init__("telegram")

    def send(self, notification: Notification, context: DeliveryContext) -> DeliveryResult:
        if not context.secret or not context.destination:
            return DeliveryResult(DeliveryStatus.FAILED, False, error_code="telegram_config_missing", error_message="Telegram token or destination is not configured")
        endpoint = f"https://api.telegram.org/bot{context.secret}/sendMessage"
        original = notification
        payload = json.dumps({"chat_id": context.destination, "text": f"{original.title}\n{original.body}"}).encode()
        try:
            with urlopen(Request(endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST"), timeout=context.timeout_seconds) as response:
                status = int(response.status)
                return DeliveryResult(DeliveryStatus.SENT if 200 <= status < 300 else DeliveryStatus.FAILED, 500 <= status < 600, provider_status_code=status)
        except HTTPError as exc:
            return DeliveryResult(DeliveryStatus.FAILED, 500 <= exc.code < 600, provider_status_code=exc.code, error_code="telegram_http_error", error_message=f"HTTP status {exc.code}")
        except (URLError, TimeoutError):
            return DeliveryResult(DeliveryStatus.UNKNOWN, False, error_code="telegram_network_unknown", error_message="Network outcome was ambiguous")


class ChannelRegistry:
    def __init__(self, adapters: dict[str, ChannelAdapter] | None = None):
        self.adapters = adapters or {}

    def adapter_for(self, channel: NotificationChannel) -> ChannelAdapter:
        return self.adapters.get(channel.channel_type, UnconfiguredAdapter(channel.channel_type))

    @staticmethod
    def default() -> "ChannelRegistry":
        return ChannelRegistry({"telegram": TelegramAdapter(), "webhook": HttpAdapter("webhook"), "http": HttpAdapter("http")})

    @staticmethod
    def context(channel: NotificationChannel, provider_request_key: str) -> DeliveryContext:
        secret = os.environ.get(channel.secret_ref) if channel.secret_ref else None
        destination = os.environ.get(channel.destination_ref) if channel.destination_ref else None
        return DeliveryContext(provider_request_key, channel.timeout_seconds, secret, destination)
