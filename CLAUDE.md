# CLAUDE.md — Pitwall Project

## What This Is

Pitwall is an F1 MCP server with 67 tools. It provides Formula 1 data to Claude via the Model Context Protocol.

## Key Architecture

- `pitwall.py` — MCP server (67 tools). Auto-degrades: 14 tools without FastF1, 67 with it.
- `signalr_client.py` — Raw SignalR Core WebSocket client for live race data during sessions.
- `decompressor.py` — Zlib decompression for CarData.z (telemetry) and Position.z (GPS).
- `merger.py` — Deep-merge for F1's incremental JSON delta format.
- `topics.py` — All 20 SignalR topics with auth/compression metadata.
- `auth_setup.py` — F1 TV Premium token setup (browser OAuth flow).

## Data Sources (all free, no API keys)

- **F1 Static API** (`livetiming.formula1.com/static/`) — 33 feeds per session, 2018-present
- **Jolpica-F1** (`api.jolpi.ca/ergast/f1/`) — Historical results 1950-present
- **FastF1** (optional) — Enhanced telemetry, plots, deep analysis
- **SignalR Core** (live) — Real-time during races, requires F1 TV auth for telemetry

## F1 TV Authentication

Token stored at `~/.f1token` and `~/Library/Application Support/fastf1/f1auth.json`.
Expires every ~4 days. Re-authenticate: `python3 auth_setup.py`.
Only needed for live telemetry (CarData.z, Position.z) during active sessions.
All post-session and historical data is free without auth.

## Sprint Weekend Gotcha

At sprint weekends, both Sprint and Race have `Type="Race"` in F1's data. The session resolver uses 3-pass priority (exact name > partial name > type) to correctly resolve "Race" to the main Race, not the Sprint. If you hit issues, pass the explicit `session_path` from `list_races`.

## Installation

```bash
pip install f1pitwall              # Lite (14 tools)
pip install "f1pitwall[full]"      # Full (67 tools)
```

## Running

```bash
f1pitwall                          # MCP stdio (Claude Code / Desktop)
f1pitwall --http                   # MCP HTTP (remote)
claude mcp add pitwall -- f1pitwall  # Register with Claude Code
```
