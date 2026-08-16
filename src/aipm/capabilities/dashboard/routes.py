from aipm.models.telemetry import HandbookRoute


def handbook_routes() -> tuple[HandbookRoute, ...]:
    return (
        HandbookRoute(
            id="emergency",
            title="Emergency",
            description="First-response checks for slow or unstable VPS behavior.",
            commands=(
                "date; uptime; free -h; df -h",
                "systemctl --failed --no-pager",
                "sudo journalctl -p err..alert --since '-30 min' --no-pager",
            ),
        ),
        HandbookRoute(
            id="resources",
            title="CPU & Memory",
            description="Current usage, load, RAM pressure, swap, and process ownership.",
            commands=(
                "sar -u 1 10",
                "free -h",
                "ps -eo pid,user,comm,%cpu,%mem,rss --sort=-%cpu | head -n 20",
            ),
        ),
        HandbookRoute(
            id="disk",
            title="Disk",
            description="Filesystem, inode, Docker, journal, cache, and full-disk recovery workflows.",
            commands=("df -hT", "df -i", "docker system df -v"),
        ),
        HandbookRoute(
            id="docker",
            title="Docker & Compose",
            description="Container lifecycle, logs, health checks, networks, volumes, and restart evidence.",
            commands=("docker ps -a", "docker stats --no-stream", "docker system df -v"),
        ),
        HandbookRoute(
            id="stack",
            title="Local AI Stack",
            description="The local-ai-packaged orchestration and known container-collision recovery guidance.",
            commands=("./manage.py start", "./manage.py stop", "docker compose ps"),
        ),
        HandbookRoute(
            id="network",
            title="Network & Tunnel",
            description="Listening sockets, HTTP checks, Docker networks, and cloudflared troubleshooting.",
            commands=("sudo ss -tulpn", "curl -I --max-time 10 https://example.com", "docker logs --tail 100 Cloudflared"),
        ),
        HandbookRoute(
            id="projects",
            title="AIPM / Git / Projects",
            description="Project discovery, Git state, health checks, backups, and guarded updates.",
            commands=("aipm doctor", "aipm discover", "aipm update <project> --dry-run"),
        ),
        HandbookRoute(
            id="security",
            title="Security & Backups",
            description="Safe change boundaries, auditability, snapshots, and review before destructive operations.",
            commands=("sudo ss -tulpn", "sudo journalctl --since today", "aipm backup <project>"),
        ),
    )
