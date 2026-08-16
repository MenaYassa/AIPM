from __future__ import annotations

import os
import time
from types import ModuleType
from typing import Any

import psutil

from aipm.models.telemetry import HostSnapshot, NetworkStats, SwapStats, TelemetryError
from aipm.services.system.service import SystemService


class HostTelemetryService:
    """Collect read-only host state using existing AIPM system telemetry."""

    def __init__(
        self,
        system_service: SystemService | None = None,
        *,
        psutil_module: ModuleType = psutil,
        os_module: Any = os,
        time_module: Any = time,
        logger: Any | None = None,
    ) -> None:
        self.system_service = system_service or SystemService()
        self.psutil = psutil_module
        self.os = os_module
        self.time = time_module
        self.logger = logger

    def snapshot(self) -> HostSnapshot:
        system = None
        host_error = None
        try:
            system = self.system_service.summary()
        except Exception as exc:  # isolate the shared system service from other host measurements
            host_error = self._error("HOST_TELEMETRY_UNAVAILABLE", "Host telemetry unavailable", exc)

        swap = self._swap()
        load = self._load()
        uptime = self._uptime()
        network = self._network()
        return HostSnapshot(
            system=system,
            swap=swap,
            load_one=load[0],
            load_five=load[1],
            load_fifteen=load[2],
            uptime_seconds=uptime,
            network=network,
            available=system is not None,
            error=host_error,
        )

    def _swap(self) -> SwapStats:
        try:
            value = self.psutil.swap_memory()
            gb = 1024**3
            return SwapStats(
                total_gb=round(value.total / gb, 2),
                used_gb=round(value.used / gb, 2),
                percent=round(value.percent, 1),
            )
        except Exception as exc:
            return SwapStats(available=False, error=self._error("SWAP_TELEMETRY_UNAVAILABLE", "Swap telemetry unavailable", exc))

    def _load(self) -> tuple[float | None, float | None, float | None]:
        try:
            values = self.os.getloadavg()
            return tuple(round(value, 2) for value in values[:3])  # type: ignore[return-value]
        except (AttributeError, OSError, ValueError) as exc:
            self._log("Load telemetry unavailable", exc)
            return None, None, None

    def _uptime(self) -> int | None:
        try:
            return max(0, int(self.time.time() - self.psutil.boot_time()))
        except Exception as exc:
            self._log("Uptime telemetry unavailable", exc)
            return None

    def _network(self) -> NetworkStats:
        try:
            interfaces = len(self.psutil.net_if_addrs())
        except Exception as exc:
            return NetworkStats(
                available=False,
                established=None,
                error=self._error("NETWORK_TELEMETRY_UNAVAILABLE", "Network telemetry unavailable", exc),
            )
        try:
            established = sum(
                connection.status == "ESTABLISHED"
                for connection in self.psutil.net_connections(kind="inet")
            )
            return NetworkStats(interfaces=interfaces, established=established)
        except Exception as exc:
            return NetworkStats(
                interfaces=interfaces,
                established=None,
                available=False,
                error=self._error("NETWORK_CONNECTIONS_UNAVAILABLE", "Network connection telemetry unavailable", exc),
            )

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        self._log(message, exc)
        return TelemetryError(code=code, message=message)

    def _log(self, message: str, exc: Exception) -> None:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
