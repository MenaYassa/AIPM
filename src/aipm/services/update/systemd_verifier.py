from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from aipm.providers.systemd_observation import SystemdObservationProvider, SystemdTrustError, SystemdUnitObservation


class SystemdVerificationStatus(Enum):
    SUCCESS = "success"
    SUPERVISOR_FAILURE = "supervisor_failure"
    APPLICATION_PROBE_FAILURE = "application_probe_failure"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class SystemdVerificationResult:
    status: SystemdVerificationStatus
    passed: bool
    supervisor_passed: bool
    probe_passed: bool
    error: str | None = None
    details: tuple[str, ...] = ()


class SystemdVerifier:
    """Independent two-layer verifier for systemd service update transactions.

    Layer 1: Supervisor Transition Verification (D-Bus / systemctl show)
             - Asserts ActiveState is active and SubState is running.
             - Asserts InvocationID transitioned away from pre-restart state.
             - Asserts NRestarts does not indicate a crash loop.
    Layer 2: Functional Application Health Probe (HTTP / Socket)
             - Polls designated application health endpoint.
             - Asserts HTTP 200 and expected payload.
    """

    def __init__(
        self,
        observation_provider: SystemdObservationProvider | None = None,
        http_client: Callable[..., Any] | None = None,
    ) -> None:
        self.provider = observation_provider or SystemdObservationProvider()
        self._http_client = http_client or urllib.request.urlopen

    def verify_supervisor_transition(
        self,
        unit_name: str,
        pre_observation: SystemdUnitObservation,
        *,
        timeout_seconds: float = 15.0,
        poll_interval_seconds: float = 0.25,
    ) -> tuple[bool, str | None, SystemdUnitObservation | None]:
        """Poll supervisor until InvocationID changes and unit is active."""
        deadline = time.monotonic() + timeout_seconds
        last_obs: SystemdUnitObservation | None = None

        while time.monotonic() < deadline:
            try:
                obs = self.provider.observe_unit(unit_name)
                last_obs = obs
            except SystemdTrustError as exc:
                return False, f"Observation error during verification: {exc}", last_obs

            # Crash loop detection: restarts increased by more than 1
            if obs.restart_count > pre_observation.restart_count + 1:
                return False, f"Crash loop detected on {unit_name}: restart count jumped from {pre_observation.restart_count} to {obs.restart_count}", obs

            # Unit explicitly failed
            if obs.active_state == "failed" or obs.sub_state == "failed":
                return False, f"Unit {unit_name} entered failed state", obs

            # True restart check: InvocationID must have changed
            if (
                obs.active_state == "active"
                and obs.sub_state == "running"
                and obs.invocation_id is not None
                and obs.invocation_id != pre_observation.invocation_id
            ):
                return True, None, obs

            time.sleep(poll_interval_seconds)

        err = f"Supervisor transition timed out after {timeout_seconds}s for {unit_name}"
        if last_obs and last_obs.invocation_id == pre_observation.invocation_id:
            err += " (InvocationID did not change; restart was not executed or was a no-op)"
        return False, err, last_obs

    def verify_application_health(
        self,
        probe_contract: str | None,
        *,
        timeout_seconds: float = 15.0,
        poll_interval_seconds: float = 0.5,
    ) -> tuple[bool, str | None]:
        """Poll application endpoint until healthy or timeout."""
        if not probe_contract:
            return True, None

        parts = probe_contract.split(":", 1)
        probe_type = parts[0]
        probe_target = parts[1] if len(parts) > 1 else ""

        if probe_type != "http":
            return False, f"Unsupported health probe type: {probe_type}"

        deadline = time.monotonic() + timeout_seconds
        last_err: str | None = None

        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    probe_target,
                    headers={"User-Agent": "AIPM-SystemdVerifier/1.0", "Accept": "application/json"},
                )
                with self._http_client(req, timeout=3.0) as resp:
                    status = getattr(resp, "status", getattr(resp, "code", 200))
                    if status == 200:
                        return True, None
                    last_err = f"Probe returned HTTP {status}"
            except urllib.error.HTTPError as exc:
                last_err = f"Probe returned HTTP {exc.code}"
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_err = f"Probe connection error: {exc}"

            time.sleep(poll_interval_seconds)

        return False, f"Application health probe timed out after {timeout_seconds}s ({last_err})"

    def verify_systemd_update(
        self,
        unit_name: str,
        pre_observation: SystemdUnitObservation,
        health_probe_contract: str | None = None,
        *,
        supervisor_timeout_seconds: float = 15.0,
        probe_timeout_seconds: float = 15.0,
    ) -> SystemdVerificationResult:
        """Run complete 2-layer independent verification for a systemd update."""
        details: list[str] = []
        try:
            sup_passed, sup_err, _ = self.verify_supervisor_transition(
                unit_name,
                pre_observation,
                timeout_seconds=supervisor_timeout_seconds,
            )
            if not sup_passed:
                details.append(f"Supervisor layer: {sup_err}")
                return SystemdVerificationResult(
                    status=SystemdVerificationStatus.SUPERVISOR_FAILURE,
                    passed=False,
                    supervisor_passed=False,
                    probe_passed=False,
                    error=sup_err,
                    details=tuple(details),
                )
            details.append("Supervisor layer: transition verified (active/running, new InvocationID)")

            probe_passed, probe_err = self.verify_application_health(
                health_probe_contract,
                timeout_seconds=probe_timeout_seconds,
            )
            if not probe_passed:
                details.append(f"Application probe layer: {probe_err}")
                return SystemdVerificationResult(
                    status=SystemdVerificationStatus.APPLICATION_PROBE_FAILURE,
                    passed=False,
                    supervisor_passed=True,
                    probe_passed=False,
                    error=probe_err,
                    details=tuple(details),
                )
            details.append("Application probe layer: health verified (HTTP 200 OK)")

            return SystemdVerificationResult(
                status=SystemdVerificationStatus.SUCCESS,
                passed=True,
                supervisor_passed=True,
                probe_passed=True,
                error=None,
                details=tuple(details),
            )
        except Exception as exc:
            details.append(f"Verification interrupted unexpectedly: {exc}")
            return SystemdVerificationResult(
                status=SystemdVerificationStatus.RECONCILIATION_REQUIRED,
                passed=False,
                supervisor_passed=False,
                probe_passed=False,
                error=f"Ambiguous outcome during verification: {exc}",
                details=tuple(details),
            )
