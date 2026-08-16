from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.core.app import Application
from aipm.mappers.telemetry_history import TelemetryHistoryMapper
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository


def storage_bytes(path: Path) -> int:
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if candidate.exists())


def count_rows(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("sample_runs", "host_samples", "container_samples", "project_samples", "tunnel_samples")
        }


def shift_sample(sample, seconds: int):
    delta = timedelta(seconds=seconds)
    run = replace(sample.run, sampled_at=sample.run.sampled_at + delta)
    host = replace(sample.host, sampled_at=sample.host.sampled_at + delta) if sample.host else None
    containers = tuple(replace(item, sampled_at=item.sampled_at + delta) for item in sample.containers)
    projects = tuple(replace(item, sampled_at=item.sampled_at + delta) for item in sample.projects)
    tunnel = replace(sample.tunnel, sampled_at=sample.tunnel.sampled_at + delta) if sample.tunnel else None
    return replace(sample, run=run, host=host, containers=containers, projects=projects, tunnel=tunnel)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure MC-2 SQLite growth using the production schema.")
    parser.add_argument("--cycles", type=int, default=120, help="Representative cycles to insert into the temporary database.")
    parser.add_argument("--interval-seconds", type=int, default=15, help="Sampling interval used for day/month projections.")
    args = parser.parse_args()
    if args.cycles <= 0 or args.interval_seconds <= 0:
        raise SystemExit("cycles and interval-seconds must be greater than zero")

    application = Application.create()
    snapshot = DashboardApi.from_application(application).telemetry.snapshot()
    sample = TelemetryHistoryMapper().to_sample(snapshot)

    with tempfile.TemporaryDirectory(prefix="aipm-mc2-measure-") as directory:
        database_path = Path(directory) / "mission_control.db"
        repository = SQLiteHistoryRepository(database_path)
        baseline = storage_bytes(database_path)
        for index in range(args.cycles):
            shifted = shift_sample(sample, index * args.interval_seconds)
            repository.save_sample(shifted.run, shifted.host, shifted.containers, shifted.projects, shifted.tunnel)
        final = storage_bytes(database_path)
        delta = max(0, final - baseline)
        samples_per_day = 86400 / args.interval_seconds
        bytes_per_cycle = delta / args.cycles
        result = {
            "database": "temporary",
            "cycles": args.cycles,
            "interval_seconds": args.interval_seconds,
            "representative_entities": {
                "containers_per_cycle": len(sample.containers),
                "projects_per_cycle": len(sample.projects),
                "host_rows_per_cycle": 1 if sample.host else 0,
                "tunnel_rows_per_cycle": 1 if sample.tunnel else 0,
            },
            "rows": count_rows(database_path),
            "schema_bytes_before_samples": baseline,
            "database_bytes_after_sampling": final,
            "measured_growth_bytes": delta,
            "measured_bytes_per_cycle": round(bytes_per_cycle, 2),
            "samples_per_day_at_interval": samples_per_day,
            "projected_growth_bytes_per_day": round(bytes_per_cycle * samples_per_day),
            "projected_growth_bytes_per_7_days": round(bytes_per_cycle * samples_per_day * 7),
            "projected_growth_bytes_per_30_days": round(bytes_per_cycle * samples_per_day * 30),
        }
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
