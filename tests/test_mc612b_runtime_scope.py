from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "ops/systemd/aipm-provenance.service"
SOCKET = ROOT / "ops/systemd/aipm-provenance.socket"


def test_trusted_runtime_is_external_and_hardened() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/aipm-provenance/current/app" in text
    assert "ExecStart=/opt/aipm-provenance/current/venv/bin/python" in text
    assert "/home/ubuntu/aipm" not in text
    assert "/home/mina" in text
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    assert "RestrictAddressFamilies=AF_UNIX" in text
    assert "CapabilityBoundingSet=" not in text
    assert "AmbientCapabilities=" not in text
    assert "SystemCallFilter=" not in text
    assert "docker" not in text.casefold()
    assert "systemctl" not in text.casefold()


def test_socket_is_observation_only_endpoint() -> None:
    text = SOCKET.read_text(encoding="utf-8")
    assert "ListenStream=/run/aipm/provenance.sock" in text
    assert "SocketUser=aipm-provenance" in text
    assert "SocketGroup=aipm-provenance-client" in text
    assert "SocketMode=0660" in text
    assert "Service=aipm-provenance.service" in text
    assert "tcp" not in text.casefold()


def test_initial_trusted_source_excludes_systemd_and_docker() -> None:
    from aipm.provenance import adapters
    assert hasattr(adapters, "HostAdapter")
    assert not hasattr(adapters, "DockerAdapter")
    assert not hasattr(adapters, "SystemdAdapter")
