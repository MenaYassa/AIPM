from __future__ import annotations

import os
import re
import time
from types import ModuleType
from typing import Any

import psutil

from aipm.models.server import FilesystemDetail, NetworkInterfaceDetail, ServerHostSnapshot
from aipm.models.telemetry import HostSnapshot, NetworkStats, SwapStats, TelemetryError
from aipm.services.system.service import SystemService


class HostTelemetryService:
    """Collect read-only host state using existing AIPM system telemetry."""

    SAFE_FILESYSTEM_MOUNTS = frozenset({"/", "/home", "/opt", "/tmp", "/var"})
    MAX_FILESYSTEMS = 16
    MAX_INTERFACES = 32
    SAFE_INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")

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

    def server_snapshot(self) -> ServerHostSnapshot:
        """Return the existing host sample plus bounded optional detail."""

        host = self.snapshot()
        filesystems, filesystem_available, filesystem_error = self._filesystems()
        interfaces, interface_available, interface_error = self._interfaces()
        states, states_available, states_error = self._connection_states()
        return ServerHostSnapshot(
            host=host,
            filesystems=filesystems,
            filesystem_available=filesystem_available,
            filesystem_error=filesystem_error,
            interfaces=interfaces,
            interface_detail_available=interface_available,
            interface_detail_error=interface_error,
            connection_states=states,
            connection_states_available=states_available,
            connection_states_error=states_error,
        )

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

    def _filesystems(self) -> tuple[tuple[FilesystemDetail, ...], bool, TelemetryError | None]:
        try:
            partitions = self.psutil.disk_partitions(all=False)
        except Exception as exc:
            return (), False, self._error("FILESYSTEM_TELEMETRY_UNAVAILABLE", "Filesystem telemetry unavailable", exc)
        details = []
        for partition in partitions:
            mountpoint = getattr(partition, "mountpoint", None)
            if mountpoint not in self.SAFE_FILESYSTEM_MOUNTS or len(details) >= self.MAX_FILESYSTEMS:
                continue
            try:
                usage = self.psutil.disk_usage(mountpoint)
                gb = 1024**3
                details.append(FilesystemDetail(
                    mountpoint=mountpoint,
                    filesystem=getattr(partition, "fstype", None) or None,
                    total_gb=round(usage.total / gb, 2),
                    used_gb=round(usage.used / gb, 2),
                    free_gb=round(usage.free / gb, 2),
                    percent=round(usage.percent, 1),
                ))
            except Exception as exc:
                self._log("Filesystem detail unavailable", exc)
        if not details:
            return (), False, TelemetryError("FILESYSTEM_DETAIL_UNAVAILABLE", "Filesystem detail unavailable from current telemetry")
        return tuple(details), True, None

    def _interfaces(self) -> tuple[tuple[NetworkInterfaceDetail, ...], bool, TelemetryError | None]:
        try:
            stats = self.psutil.net_if_stats()
            counters = self.psutil.net_io_counters(pernic=True)
        except Exception as exc:
            return (), False, self._error("NETWORK_INTERFACE_DETAIL_UNAVAILABLE", "Network interface detail unavailable", exc)
        details = []
        for name in sorted(stats):
            if not self.SAFE_INTERFACE_NAME.fullmatch(name) or len(details) >= self.MAX_INTERFACES:
                continue
            stat = stats[name]
            counter = counters.get(name)
            details.append(NetworkInterfaceDetail(
                name=name,
                is_up=getattr(stat, "isup", None),
                rx_bytes=getattr(counter, "bytes_recv", None) if counter else None,
                tx_bytes=getattr(counter, "bytes_sent", None) if counter else None,
            ))
        if not details:
            return (), False, TelemetryError("NETWORK_INTERFACE_DETAIL_UNAVAILABLE", "Network interface detail unavailable from current telemetry")
        return tuple(details), True, None

    def _connection_states(self) -> tuple[tuple[tuple[str, int], ...], bool, TelemetryError | None]:
        try:
            counts: dict[str, int] = {}
            for connection in self.psutil.net_connections(kind="inet"):
                state = str(getattr(connection, "status", "UNKNOWN"))
                counts[state] = counts.get(state, 0) + 1
            return tuple(sorted(counts.items())), True, None
        except Exception as exc:
            return (), False, self._error("NETWORK_CONNECTION_STATES_UNAVAILABLE", "Network connection states unavailable", exc)

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        self._log(message, exc)
        return TelemetryError(code=code, message=message)

    def _log(self, message: str, exc: Exception) -> None:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
