# MC audit live findings — 2026-08-28

## Live vpanel dashboard route

URL: https://vpanel.03092017.xyz/

The dashboard is reachable without a login wall and identifies itself as AIPM Mission Control / Handbook 2.0. It displays a read-only cockpit and explicitly states: “Observe first. Change only through explicit future workflows.” Navigation exposes Dashboard, Server, Docker, Projects, Systemd, Logs, Incidents, History, Notifications, Settings, and AI Agent. The initial page showed “Connecting to local agent…” and placeholder/loading states for service pulse, containers, resource history, project inventory, event stream, incidents, and notification safety. It displays read-only posture, 15-second refresh cadence, remote tunnel API not queried, and no action controls.

## Live vpanel Server route

URL: https://vpanel.03092017.xyz/#/server

The Server route was live and sampled at approximately 01:51:16 AM. It reported hostname `agent`, Linux, kernel `6.17.0-1020-oracle`, architecture `aarch64`, and Python `3.12.3`. Current metrics were CPU 60%, memory 56%, swap 78.9%, root disk 54.8%, load 0.83/0.69/0.89, and 31 interfaces / 49 established connections / 6 states. It displayed 60 persisted host-history samples for the 1H view. Health showed stale Service Pulse, 10 open incidents, deferred warnings, and unavailable resource-warning projection. The route remained read-only and reported fresh current host data.

## Live vpanel Docker route

URL: https://vpanel.03092017.xyz/#/docker

The Docker route was live and sampled at approximately 01:51:53 AM. It displayed fresh read-only project groups and containers. Visible group count was 8, with 27 containers shown. The page exposed container state, health, CPU, memory, and restarts, and described the source as an existing Docker provider boundary. No mutation controls were used or observed in the inspected viewport.

## Live vpanel Projects route

URL: https://vpanel.03092017.xyz/#/projects

The Projects route was live and fresh. It reported one runtime-backed associated application, `local-ai-packaged`, with 20 components, 20 running, 20 healthy, and 0 missing health checks. It showed seven runtime-only groups including `cairo-metro-map`, `cloudflared`, `color-mixer`, `handbook`, `omniroute`, `openvpn`, and `product-tracker`; these were observable but unresolved as local project identities. It also showed four local source candidates (`aipm`, `EAG`, `fastsdcpu`, `invoicing`) without runtime association. The selected `local-ai-packaged` detail showed exact Compose identity evidence and a Git posture marked `dirty`, branch `main`, ahead 0 / behind 65, with no conflicts observed, plus observed Compose posture. This is live application evidence and not a direct assertion about the AIPM repository checkout.

## Live vpanel Systemd route

URL: https://vpanel.03092017.xyz/#/systemd

The Systemd route was live and sampled at approximately 01:53:11 AM. It displayed seven allow-listed units, with four entries in an unavailable state and unknown enablement, and three FastSD units shown as loaded/running/active/enabled. The page explicitly described itself as bounded read-only Systemd observation and stated that lifecycle controls, command output, start/stop/restart/reload/enable/disable/reset-failed, shell, and command controls are not available. No unit action was invoked.

## Live vpanel Logs route

URL: https://vpanel.03092017.xyz/#/logs

The Logs route was live and fresh. It exposed bounded source, time-window, severity, and page-size selectors, with a stated result bound and opaque next cursor. The selected AIPM Dashboard source returned 100 lines / 10,423 bytes, truncated, including an error traceback and info entries. Private paths were redacted as `[REDACTED_PATH]`, with `private_path` redaction evidence. The page stated that there are no download, clear, rotate, restart, or remediation controls. No state-changing control was used.

## Live vpanel Incidents route

URL: https://vpanel.03092017.xyz/#/incidents

The Incidents route was live and sampled at approximately 01:53:27 AM. It showed 10 persisted incidents with bounded pagination and “View persisted timeline” controls. The page stated there are no acknowledgement or remediation controls. Visible incidents included container health changes for `product-tracker` and stopped-container incidents for `product-tracker`, `whisper`, `cairo_metro_map`, and `color_mixer`. Incident states were open, warning/high severity, and timestamps were displayed. Timeline and event-detail panels were read-only and unselected in the initial view.

## Live vpanel History route

URL: https://vpanel.03092017.xyz/#/history

The History route was live and sampled at approximately 01:53:27 AM. It exposed bounded comparison controls for Host, Container, Project, and Tunnel, resource identity, and 1-hour/6-hour/24-hour/7-day windows. The default host comparison showed a baseline at 2026-08-27T01:53:10+00:00 and current at 2026-08-28T01:52:33+00:00, with field-level status, before/after values, and deltas. It explicitly stated missing data is not converted to zero. This confirmed a functioning read-only comparison surface.

## Live vpanel Notifications route

URL: https://vpanel.03092017.xyz/#/notifications

The Notifications route was live and sampled at approximately 01:54:00 AM. It explicitly states “Coming in MC-6.x” and that Notification Safety remains available on the Dashboard through existing read-only audit APIs. It also states notifications remain disabled and no provider or channel is activated. No delivery control was exposed or used.

## Live vpanel Settings route

URL: https://vpanel.03092017.xyz/#/settings

The Settings route was live and sampled at approximately 01:54:00 AM. It reported version `0.1.0`, commit `Unknown`, and state `ok`. Deployment posture was explicitly labeled required posture rather than live deployment discovery: binding `loopback_only_required`, public ingress `not_observed`, and permanent service `not_observed`. The read-only boundary showed SQLite `read_only`, query-only `true`, filesystem write boundary required, schema mutation prohibited, and checkpointing prohibited. Telemetry was enabled/fresh at 60s; MC-3/events was enabled/stale at 15s. Notification posture was disabled, provider disabled, audit status unavailable, with zero configured/enabled channels and policies.

## Live vpanel AI Agent route

URL: https://vpanel.03092017.xyz/#/ai-agent

The AI Agent route was live and labeled a future advisor, but it exposed only a read-only bounded assessment. It stated “Cloudflare Access edge,” “No action controls,” and “service-owned evaluation.” Live mode was selected with an explicit fixture mode alongside it, plus a single refresh action. At approximately 01:54:16 AM, the displayed live assessment was fresh and available, scope host, with 18/18 evidence coverage, zero stale/unavailable/invalid/omitted records, zero findings, zero recommendations, and zero uncertainty records. The three resource-history metrics were complete with six valid points across 300 seconds at 60-second cadence; peaks shown were CPU 32.7%, memory 56%, disk 54.8%. The UI explicitly said live mode uses one read-only GET assessment with no polling and fixture mode never mixes with live data.

## Direct live GET /api/advisor

URL: https://vpanel.03092017.xyz/api/advisor

A direct safe GET returned a JSON response successfully during inspection. The response had `schema_version` 1.0, `available: true`, `status: fresh`, `scope: host`, matching `evaluation_time` and `generated_at` at `2026-08-28T01:54:33+00:00`, empty findings/recommendations/uncertainties/provenance/links, and evidence coverage `history` expected 18 / observed 18 with no stale, unavailable, invalid, or omitted records. `resource_history_summary` contained deterministic CPU, memory, disk order; each had six valid points, 300-second span, 60-second cadence, and a bounded peak/timestamp pair. `next_cursor` was null. No action or mutation behavior was exposed.

## Direct live GET /api/history/host?range=1h&limit=10

This known read-only endpoint returned `available: true`, `status: ok`, `error: null`, and exactly 10 host points. Points were timestamped at one-minute intervals from 00:55:10 through 01:04:10 UTC in the response, with bounded CPU, memory, disk, load, swap, network-count, hostname, and availability fields. The response confirmed persisted history availability and an explicit limit of 10.

## Direct live GET /api/events?range=24h&limit=10

The dashboard’s apparent events query returned `available: false`, `status: unavailable`, `error: "Invalid event query"`, and an empty events list for this exact bounded query. This is a live observable event-API issue or query-contract mismatch, not proof that the underlying event store is absent; the dashboard itself displayed event loading/unavailable state during the initial route observation.

## Direct live GET /api/incidents?range=7d&status=open&limit=10

This known read-only endpoint returned `available: true`, `status: ok`, `error: null`, and 10 open incidents. The records included IDs, incident keys, titles, severity, status, timestamps, resource identity, summaries, and linked event evidence. Examples included open warning `product-tracker` health-change incidents and high-severity stopped-container incidents for `product-tracker`, `whisper`, `cairo_metro_map`, and `color_mixer`. No mutation field or control was invoked.

## Direct live GET /api/notifications?limit=10

This known read-only endpoint returned `available: false`, `status: unavailable`, `error: "Notification data unavailable"`, an empty notifications list, and empty metrics. This matches the disabled/unavailable posture shown in the UI.

## Direct live GET /api/notification-metrics

This known read-only endpoint returned the same safe unavailable response: `available: false`, `status: unavailable`, `error: "Notification data unavailable"`, empty notifications, and empty metrics. No delivery or retry operation was triggered.

## Resolved aggregate Dashboard observation

URL: https://vpanel.03092017.xyz/#/dashboard

After returning to the dashboard and allowing the existing client fetches to resolve, the aggregate cockpit displayed host agent online, Docker daemon 27 running, Cloudflared local agent up, current CPU 40.3%, memory 56.1%, root disk 54.8%, load 0.83, uptime 2d 8h 20m, and 41 connections across 31 interfaces. Telemetry was fresh (0 seconds old); MC-3 was stale (2,361 seconds old), so the overall Service Pulse was stale. The dashboard showed 27 running containers and 0 unhealthy, but the container resource observations were themselves stale at 1,557 seconds. It displayed 60 persisted 1H host-history samples, five configured search paths with project freshness `never_sampled` and no Git/Compose projects found in that aggregate section, 14 recent deterministic MC-3 events, and 10 open incidents. The handbook topics, 15-second refresh cadence, remote tunnel API not queried, read-only posture, and absence of lifecycle/action controls remained visible. This demonstrates the dashboard can resolve live data while also surfacing stale/deferred portions rather than claiming uniform health.

## Direct live GET /api/events?range=24h&limit=50

The exact frontend events query with its documented limit returned `available: true`, `status: ok`, `error: null`, and a bounded events list. It included deterministic health-finding and project Git-state events for the `aipm` project plus container start/stop/health transitions. The earlier `limit=10` response was therefore a small-limit validation issue or query-contract edge case, not a general event-stream outage.

## Direct live GET /healthz

The known health endpoint returned `{"status":"ok"}`.

## Scope note

These observations establish reachability and visible read-only behavior only. They do not by themselves prove every backend route, producer topology, deployment commit, or historical milestone is complete or published.
