"""
Pitwall — F1 Data MCP Server for Claude

The most comprehensive Formula 1 MCP server. 67 tools covering
race results, telemetry, tyre strategy, pit stops, weather, race control,
driver comparisons, speed traps, and historical data back to 1950.

Two modes:
  Lite  — 14 tools, no heavy dependencies, free data only
  Full  — 67 tools, includes FastF1 plots and deep analysis

Usage:
  claude mcp add pitwall -- python3 pitwall.py
  python3 pitwall.py              # stdio (Claude Code / Claude Desktop)
  python3 pitwall.py --http       # HTTP (remote MCP)

https://github.com/darshjoshi/pitwall
"""

import json
import os
import sys
import zlib
import base64
import copy
import requests
from collections import defaultdict
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Try importing FastF1 — if available, register full tool suite
try:
    import fastf1
    import fastf1.plotting
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mcp.types import ImageContent
    import numpy as np
    import io

    FASTF1_AVAILABLE = True

    # Setup FastF1 cache
    _cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    os.makedirs(_cache_dir, exist_ok=True)
    fastf1.Cache.enable_cache(_cache_dir)
except ImportError:
    FASTF1_AVAILABLE = False

# Initialize server
mcp = FastMCP(
    "Pitwall",
    instructions="""Pitwall — F1 data command center with 67 tools.

HOW TO USE:
1. Use list_races(year) to find sessions. Race names are fuzzy-matched: 'china', 'shanghai', 'chinese' all work.
2. Query data with specific tools: get_standings, get_telemetry, get_tyre_strategy, etc.
3. Default year is 2026. Use 'last year' as 2025.

DRIVER CODES: VER=Verstappen, HAM=Hamilton, NOR=Norris, LEC=Leclerc, ANT=Antonelli, RUS=Russell, PIA=Piastri, BEA=Bearman, GAS=Gasly, LAW=Lawson, HAD=Hadjar, SAI=Sainz, ALO=Alonso, OCO=Ocon, BOT=Bottas, ALB=Albon, HUL=Hulkenberg, STR=Stroll, COL=Colapinto, LIN=Lindblad, PER=Perez.

TOOL ROUTING:
- Who won / results / standings → get_standings
- Lap times / pace → get_lap_times
- Speed on a specific lap / telemetry → get_telemetry(driver, lap=N)
- Tyre strategy / compounds → get_tyre_strategy
- Pit stops / fastest pit → get_pit_stops
- Flags / penalties / safety car → get_race_control
- Weather / rain / temperature → get_weather
- Top speed / speed traps → get_speed_traps
- Compare two drivers → get_driver_comparison
- Championship standings 1950+ → get_championship_standings
- Historical results 1950+ → get_historical_results
- Visual speed trace plot → plot_telemetry_comparison (FastF1 required)
- Gear shift map → plot_gear_shifts (FastF1 required)

UPGRADE: If a user asks for plots, detailed stint analysis, or other FastF1 features and those tools are not available, tell them: "That feature requires the full install. Run: pip install \"f1pitwall[full]\" and restart Claude."

CROSS-YEAR COMPARISON: All tools accept a year parameter. Compare the same driver at the same track across years (e.g. 'VER lap 25 at China 2025 vs 2026').

SPRINT WEEKENDS: When querying a sprint weekend, both Sprint and Race have the same internal type. The resolver prefers exact name match, so session_type='Race' gets the main race, not the sprint. For the sprint specifically, use session_type='Sprint'.

DATA COVERAGE: Static API tools cover 2018-present with 33 feeds per session. Jolpica covers 1950-present for historical results and championships. All data is free — no API keys needed.""",
)


# =============================================================================
# STATIC API — Free F1 archive (2018-present, no auth needed)
# =============================================================================

STATIC_BASE = "https://livetiming.formula1.com/static"
_http = requests.Session()
_http.headers.update({"User-Agent": "Pitwall/1.0"})


def _get_json(path: str) -> dict:
    resp = _http.get(f"{STATIC_BASE}/{path}", timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8-sig"
    return resp.json()


def _find_session(year: int, race: str, session_type: str = "Race") -> tuple:
    """Find session path by fuzzy matching race name."""
    data = _get_json(f"{year}/Index.json")
    race_lower = race.lower()
    session_lower = session_type.lower()

    for m in data.get("Meetings", []):
        name = m.get("Name", "").lower()
        location = m.get("Location", "").lower()
        country = m.get("Country", {}).get("Name", "").lower()

        if race_lower not in name and race_lower not in location and race_lower not in country:
            continue

        sessions = m.get("Sessions", [])

        # Priority: exact name > partial name > type match
        for s in sessions:
            if s.get("Name", "").lower() == session_lower:
                return s["Path"], m["Name"]
        for s in sessions:
            if session_lower in s.get("Name", "").lower() and s.get("Name", "").lower() != session_lower:
                return s["Path"], m["Name"]
        for s in sessions:
            if s.get("Type", "").lower() == session_lower and s.get("Name", "").lower() != session_lower:
                return s["Path"], m["Name"]

    return None, None


def _get_keyframe(session_path: str, feed_name: str) -> dict:
    feeds = _get_json(f"{session_path}Index.json").get("Feeds", {})
    if feed_name not in feeds:
        raise ValueError(f"Feed '{feed_name}' not available. Available: {list(feeds.keys())}")

    url = f"{STATIC_BASE}/{session_path}{feeds[feed_name]['KeyFramePath']}"
    resp = _http.get(url, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8-sig"

    if feed_name.endswith(".z"):
        raw = resp.json()
        if isinstance(raw, str):
            return json.loads(zlib.decompress(base64.b64decode(raw), -zlib.MAX_WBITS))
        return raw
    return resp.json()


def _driver_map(session_path: str) -> dict:
    drivers = _get_keyframe(session_path, "DriverList")
    return {
        num: {"name": d["FullName"], "team": d.get("TeamName", "?"), "tla": d.get("Tla", "?")}
        for num, d in drivers.items()
        if isinstance(d, dict) and "FullName" in d
    }


def _deep_merge(base, update):
    if not isinstance(base, dict) or not isinstance(update, dict):
        return update
    merged = copy.copy(base)
    for k, v in update.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


CAR_CHANNELS = {0: "rpm", 2: "speed", 3: "gear", 4: "throttle", 5: "brake", 45: "drs"}


def _parse_car_data(data: dict) -> list:
    results = []
    for entry in data.get("Entries", []):
        ts = entry.get("Utc", "")
        for num, car in entry.get("Cars", {}).items():
            ch = car.get("Channels", {})
            row = {"timestamp": ts, "driver_number": num}
            for cid, name in CAR_CHANNELS.items():
                row[name] = ch.get(str(cid), ch.get(cid))
            results.append(row)
    return results


def _parse_stream_line(line: str):
    line = line.strip().rstrip("\x1e")
    if not line:
        return None, None
    if "\x1e" in line:
        parts = line.split("\x1e", 1)
        return parts[0].strip(), parts[1].strip()
    for i, ch in enumerate(line):
        if ch in ('{', '[', '"'):
            return line[:i].strip(), line[i:]
    return None, None


def _find_driver_num(driver: str, dm: dict) -> str | None:
    d = driver.upper()
    for num, info in dm.items():
        if info["tla"] == d or num == d:
            return num
    return None


# =============================================================================
# CORE TOOLS — Calendar & Browsing
# =============================================================================

@mcp.tool()
def list_seasons() -> str:
    """List all available F1 seasons (2018-present)."""
    try:
        lines = []
        for year in range(2018, datetime.now().year + 1):
            try:
                n = len(_get_json(f"{year}/Index.json").get("Meetings", []))
                lines.append(f"  {year}: {n} events")
            except Exception:
                pass
        return "Available seasons:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_races(year: int) -> str:
    """List all races and sessions for a season with dates and session paths.

    Args:
        year: Season year (2018-2026)
    """
    try:
        meetings = _get_json(f"{year}/Index.json").get("Meetings", [])
        result = f"=== F1 {year} — {len(meetings)} events ===\n\n"
        for m in meetings:
            result += f"{m['Name']} ({m['Location']}, {m['Country']['Name']})\n"
            for s in m.get("Sessions", []):
                result += f"  {s['Name']:20s} ({s['Type']}) {s['StartDate']}\n"
            result += "\n"
        return result
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_race_info(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get session details and list of available data feeds.

    Args:
        year: Season year
        race: Race name (partial match — 'china', 'monaco', 'silverstone')
        session_type: 'Race', 'Qualifying', 'Sprint', 'Practice 1', etc.
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return f"No '{session_type}' found for '{race}' in {year}"
        info = _get_keyframe(path, "SessionInfo")
        feeds = _get_json(f"{path}Index.json").get("Feeds", {})
        result = f"=== {info.get('Meeting', {}).get('OfficialName', race_name)} ===\n"
        result += f"Session: {info.get('Name', session_type)}\n"
        result += f"Path: {path}\n"
        result += f"Available feeds ({len(feeds)}): {', '.join(sorted(feeds.keys()))}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# CORE TOOLS — Race Results
# =============================================================================

@mcp.tool()
def get_standings(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get race classification — positions, gaps, best laps, pit stops, retirements.

    Args:
        year: Season year (2018-2026)
        race: Race name (partial match — 'china', 'australia', 'monaco')
        session_type: 'Race', 'Qualifying', 'Sprint', etc.
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return f"No '{session_type}' found for '{race}' in {year}"

        dm = _driver_map(path)
        timing = _get_keyframe(path, "TimingData")
        classified = []
        for num, data in timing.get("Lines", {}).items():
            if not isinstance(data, dict) or "Position" not in data:
                continue
            d = dm.get(num, {"name": f"#{num}", "team": "?", "tla": "?"})
            classified.append({
                "pos": int(data["Position"]), "tla": d["tla"], "name": d["name"],
                "team": d["team"], "gap": data.get("GapToLeader", ""),
                "best": data.get("BestLapTime", {}).get("Value", ""),
                "laps": data.get("NumberOfLaps", 0),
                "pits": data.get("NumberOfPitStops", 0),
                "ret": data.get("Retired", False),
            })
        classified.sort(key=lambda x: x["pos"])

        result = f"=== {race_name} {year} — {session_type} ===\n\n"
        for c in classified:
            s = " (RET)" if c["ret"] else ""
            result += f"P{c['pos']:>2} {c['tla']:>3} {c['name']:30s} {c['team']:20s} Gap: {c['gap']:>10} Best: {c['best']:>10} Pits: {c['pits']}{s}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# CORE TOOLS — Lap Times
# =============================================================================

@mcp.tool()
def get_lap_times(year: int = 2026, race: str = "", driver: str = "",
                  session_type: str = "Race", lap_start: int = 1, lap_end: int = 999) -> str:
    """Get lap-by-lap times for one or all drivers. Filterable by lap range.

    Args:
        year: Season year
        race: Race name (partial match)
        driver: Driver TLA (e.g. 'VER') or empty for all
        session_type: Session type
        lap_start: First lap to include
        lap_end: Last lap to include
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"

        dm = _driver_map(path)
        target = _find_driver_num(driver, dm) if driver else None
        feeds = _get_json(f"{path}Index.json").get("Feeds", {})
        sp = feeds.get("TimingData", {}).get("StreamPath", "")
        if not sp:
            return "TimingData stream not available"

        resp = _http.get(f"{STATIC_BASE}/{path}{sp}", timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8-sig"

        state = _get_keyframe(path, "TimingData")
        laps = defaultdict(list)
        cur = {}
        prev_lap_num = {}

        for line in resp.text.strip().split("\n"):
            ts, ds = _parse_stream_line(line)
            if not ds:
                continue
            try:
                state = _deep_merge(state, json.loads(ds))
            except json.JSONDecodeError:
                continue
            for num, info in state.get("Lines", {}).items():
                if target and num != target:
                    continue
                if not isinstance(info, dict):
                    continue
                lap_num = info.get("NumberOfLaps")
                lt = info.get("LastLapTime", {})
                val = lt.get("Value", "") if isinstance(lt, dict) else ""
                if not val or not lap_num:
                    continue
                # Record when lap number increments (new lap completed)
                if lap_num != prev_lap_num.get(num):
                    prev_lap_num[num] = lap_num
                    if lap_start <= lap_num <= lap_end:
                        d = dm.get(num, {"tla": f"#{num}"})
                        laps[num].append(f"  Lap {lap_num:>2}: {val}")
                    cur[num] = val

        result = f"=== {race_name} {year} — Lap Times ===\n\n"
        for num, entries in laps.items():
            d = dm.get(num, {"tla": f"#{num}", "team": "?"})
            result += f"{d['tla']} ({d['team']}):\n" + "\n".join(entries) + "\n\n"
        return result if laps else "No lap time data found"
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# CORE TOOLS — Telemetry (per-lap)
# =============================================================================

@mcp.tool()
def get_telemetry(driver: str, year: int = 2026, race: str = "",
                  lap: int = 0, session_type: str = "Race") -> str:
    """Get car telemetry — speed, RPM, throttle, brake, gear, DRS for a specific lap.

    Returns ~60-90 samples at ~4Hz. Set lap=0 to see available laps.

    Args:
        driver: Driver TLA (e.g. 'VER', 'HAM') — required
        year: Season year (2018-2026)
        race: Race name (partial match)
        lap: Lap number (0 = show available laps)
        session_type: Session type
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"

        dm = _driver_map(path)
        target = _find_driver_num(driver, dm)
        if not target:
            return f"Driver '{driver}' not found. Available: {', '.join(d['tla'] for d in dm.values())}"

        d_info = dm[target]
        feeds = _get_json(f"{path}Index.json").get("Feeds", {})

        # Build lap boundaries from TimingData stream
        resp = _http.get(f"{STATIC_BASE}/{path}{feeds['TimingData']['StreamPath']}", timeout=30)
        resp.raise_for_status()
        resp.encoding = "utf-8-sig"

        state = _get_keyframe(path, "TimingData")
        boundaries = {}
        cur_lap = None

        for line in resp.text.strip().split("\n"):
            ts, ds = _parse_stream_line(line)
            if not ds:
                continue
            try:
                state = _deep_merge(state, json.loads(ds))
            except json.JSONDecodeError:
                continue
            info = state.get("Lines", {}).get(target, {})
            if not isinstance(info, dict):
                continue
            dl = info.get("NumberOfLaps")
            if dl and dl != cur_lap:
                if cur_lap and cur_lap in boundaries:
                    boundaries[cur_lap]["end"] = ts
                boundaries[dl] = {"start": ts, "end": None}
                cur_lap = dl

        if lap == 0:
            total = max(boundaries.keys()) if boundaries else 0
            return (
                f"{d_info['tla']} ({d_info['team']}) — {race_name} {year}\n"
                f"Total laps: {total}\n"
                f"Available: {sorted(boundaries.keys())}\n\n"
                f"Set lap=N for full telemetry trace."
            )

        if lap not in boundaries:
            return f"Lap {lap} not found. Available: {sorted(boundaries.keys())}"

        b = boundaries[lap]

        # Stream CarData.z for this lap
        car_resp = _http.get(f"{STATIC_BASE}/{path}{feeds['CarData.z']['StreamPath']}", timeout=30)
        car_resp.raise_for_status()
        car_resp.encoding = "utf-8-sig"

        samples = []
        in_window = False
        for line in car_resp.text.strip().split("\n"):
            ts, ds = _parse_stream_line(line)
            if not ds:
                continue
            if b["start"] and ts >= b["start"]:
                in_window = True
            if b["end"] and ts > b["end"]:
                break
            if not in_window:
                continue
            try:
                raw = json.loads(ds)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, str):
                try:
                    raw = json.loads(zlib.decompress(base64.b64decode(raw), -zlib.MAX_WBITS))
                except Exception:
                    continue
            for e in _parse_car_data(raw) if isinstance(raw, dict) else []:
                if e["driver_number"] == target:
                    samples.append(e)
            if len(samples) >= 100:
                break

        speeds = [s["speed"] for s in samples if s.get("speed") and s["speed"] > 0]
        v = lambda val: str(val) if val is not None else "?"

        result = f"=== {d_info['tla']} ({d_info['team']}) — Lap {lap} ===\n"
        result += f"Race: {race_name} {year} | Samples: {len(samples)}\n\n"
        if speeds:
            result += f"Max: {max(speeds)} km/h | Min: {min(speeds)} km/h | Avg: {sum(speeds)/len(speeds):.1f} km/h\n\n"
        result += f"{'Time':>14} {'Spd':>5} {'RPM':>6} {'Thr':>4} {'Brk':>4} {'Gear':>4} {'DRS':>4}\n"
        result += "-" * 45 + "\n"
        for s in samples[:80]:
            result += f"{str(s.get('timestamp',''))[:14]:>14} {v(s.get('speed')):>5} {v(s.get('rpm')):>6} {v(s.get('throttle')):>4} {v(s.get('brake')):>4} {v(s.get('gear')):>4} {v(s.get('drs')):>4}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# CORE TOOLS — Strategy, Pit Stops, Race Control, Weather, Speed
# =============================================================================

@mcp.tool()
def get_tyre_strategy(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get tyre strategy for every driver — compound, stint length, new/used tyres.

    Args:
        year: Season year
        race: Race name (partial match)
        session_type: Session type
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"
        dm = _driver_map(path)
        tyres = _get_keyframe(path, "TyreStintSeries").get("Stints", {})
        timing = _get_keyframe(path, "TimingData").get("Lines", {})
        abbr = {"SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W"}
        classified = sorted((int(d["Position"]), n) for n, d in timing.items() if isinstance(d, dict) and "Position" in d)

        result = f"=== {race_name} {year} — Tyre Strategy ===\n\n"
        for pos, num in classified:
            d = dm.get(num, {"tla": f"#{num}"})
            stints = tyres.get(num, [])
            parts = [f"{abbr.get(s.get('Compound','?'), '?')}{s.get('TotalLaps','?')}({'N' if s.get('New')=='true' else 'U'})"
                     for s in stints if isinstance(s, dict)]
            result += f"P{pos:>2} {d['tla']:>3}: {' > '.join(parts) if parts else 'N/A'}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_pit_stops(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get all pit stops sorted by fastest stop time.

    Args:
        year: Season year
        race: Race name (partial match)
        session_type: Session type
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"
        dm = _driver_map(path)
        pit_times = _get_keyframe(path, "PitStopSeries").get("PitTimes", {})
        stops = []
        for num, sl in pit_times.items():
            d = dm.get(num, {"tla": f"#{num}", "team": "?"})
            for s in sl:
                ps = s.get("PitStop", {})
                stops.append({"tla": d["tla"], "team": d["team"], "lap": ps.get("Lap", "?"),
                              "time": ps.get("PitStopTime", "?"), "lane": ps.get("PitLaneTime", "?")})
        stops.sort(key=lambda x: float(x["time"]) if x["time"] != "?" else 999)

        result = f"=== {race_name} {year} — Pit Stops ({len(stops)}) ===\n\n"
        for s in stops:
            result += f"{s['tla']:>3} ({s['team']:20s}) Lap {s['lap']:>3} — {s['time']:>5}s (lane: {s['lane']}s)\n"
        return result
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_race_control(year: int = 2026, race: str = "", session_type: str = "Race", category: str = "") -> str:
    """Get race control messages — flags, penalties, safety cars, investigations.

    Args:
        year: Season year
        race: Race name (partial match)
        session_type: Session type
        category: Filter: 'Flag', 'SafetyCar', 'Drs', 'Other', or empty for all
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"
        msgs = _get_keyframe(path, "RaceControlMessages").get("Messages", [])
        ml = msgs if isinstance(msgs, list) else list(msgs.values())
        if category:
            cl = category.lower()
            ml = [m for m in ml if isinstance(m, dict) and (m.get("Category", "").lower() == cl or m.get("Flag", "").lower() == cl)]
        result = f"=== {race_name} {year} — Race Control ({len(ml)} msgs) ===\n\n"
        for m in ml:
            if isinstance(m, dict):
                flag = m.get("Flag", "")
                cat = m.get("Category", "")
                prefix = f"[{flag}]" if flag else f"[{cat}]"
                result += f"Lap {m.get('Lap', '?'):>2} {prefix:>15} {m.get('Message', '')}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_weather(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get weather conditions during a session — temperature, rain, wind, humidity.

    Args:
        year: Season year
        race: Race name (partial match)
        session_type: Session type
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"
        w = _get_keyframe(path, "WeatherData")
        return (
            f"=== {race_name} {year} — Weather ===\n\n"
            f"Air: {w.get('AirTemp', '?')}C | Track: {w.get('TrackTemp', '?')}C\n"
            f"Humidity: {w.get('Humidity', '?')}% | Rain: {'Yes' if w.get('Rainfall', '0') != '0' else 'No'}\n"
            f"Wind: {w.get('WindSpeed', '?')} km/h @ {w.get('WindDirection', '?')}deg\n"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_speed_traps(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get speed trap readings at 4 measurement points (I1, I2, FL, ST) per driver.

    Args:
        year: Season year
        race: Race name (partial match)
        session_type: Session type
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"
        dm = _driver_map(path)
        lines = _get_keyframe(path, "TimingData").get("Lines", {})
        data = []
        for num, d in lines.items():
            if not isinstance(d, dict) or "Position" not in d:
                continue
            dr = dm.get(num, {"tla": f"#{num}", "team": "?"})
            sp = d.get("Speeds", {})
            if sp:
                data.append({"pos": int(d["Position"]), "tla": dr["tla"], "team": dr["team"],
                             "I1": sp.get("I1", {}).get("Value", ""), "I2": sp.get("I2", {}).get("Value", ""),
                             "FL": sp.get("FL", {}).get("Value", ""), "ST": sp.get("ST", {}).get("Value", "")})
        data.sort(key=lambda x: x["pos"])
        result = f"=== {race_name} {year} — Speed Traps ===\n\n"
        result += f"{'Pos':>3} {'Driver':>6} {'Team':20s} {'I1':>5} {'I2':>5} {'FL':>5} {'ST':>5}\n" + "-" * 50 + "\n"
        for s in data:
            result += f"P{s['pos']:>2} {s['tla']:>6} {s['team']:20s} {s['I1']:>5} {s['I2']:>5} {s['FL']:>5} {s['ST']:>5}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# CORE TOOLS — Driver Comparison
# =============================================================================

@mcp.tool()
def get_driver_comparison(driver_a: str, driver_b: str, year: int = 2026,
                          race: str = "", session_type: str = "Race") -> str:
    """Compare two drivers head-to-head — position, pace, strategy, pit stops.

    Args:
        driver_a: First driver TLA (e.g. 'VER')
        driver_b: Second driver TLA (e.g. 'HAM')
        year: Season year
        race: Race name (partial match)
        session_type: Session type
    """
    try:
        path, race_name = _find_session(year, race, session_type)
        if not path:
            return "No session found"
        dm = _driver_map(path)
        na, nb = _find_driver_num(driver_a, dm), _find_driver_num(driver_b, dm)
        if not na:
            return f"Driver '{driver_a}' not found"
        if not nb:
            return f"Driver '{driver_b}' not found"

        timing = _get_keyframe(path, "TimingData").get("Lines", {})
        tyres = _get_keyframe(path, "TyreStintSeries").get("Stints", {})
        pits = _get_keyframe(path, "PitStopSeries").get("PitTimes", {})
        abbr = {"SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W"}

        result = f"=== {race_name} {year} — {dm[na]['tla']} vs {dm[nb]['tla']} ===\n\n"
        for num in [na, nb]:
            d = dm[num]
            data = timing.get(num, {})
            if not isinstance(data, dict):
                continue
            result += f"{d['tla']} ({d['team']}):\n"
            result += f"  Position: P{data.get('Position', '?')}\n"
            result += f"  Gap: {data.get('GapToLeader', 'N/A')}\n"
            result += f"  Best lap: {data.get('BestLapTime', {}).get('Value', 'N/A')}\n"
            result += f"  Laps: {data.get('NumberOfLaps', '?')} | Pits: {data.get('NumberOfPitStops', '?')}\n"
            stints = tyres.get(num, [])
            parts = [f"{abbr.get(s.get('Compound', '?'), '?')}{s.get('TotalLaps', '?')}"
                     for s in stints if isinstance(s, dict)]
            result += f"  Strategy: {' > '.join(parts)}\n"
            for p in pits.get(num, []):
                ps = p.get("PitStop", {})
                result += f"  Pit: Lap {ps.get('Lap', '?')} — {ps.get('PitStopTime', '?')}s\n"
            result += "\n"
        return result
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# CORE TOOLS — Historical (Jolpica-F1, 1950-present)
# =============================================================================

JOLPICA = "https://api.jolpi.ca/ergast/f1"


@mcp.tool()
def get_historical_results(year: int = 0, race: str = "", driver: str = "") -> str:
    """Get historical F1 race results from 1950 to present.

    Args:
        year: Specific year (0 = current season)
        race: Circuit name (e.g. 'monza', 'monaco')
        driver: Driver ID (e.g. 'verstappen', 'hamilton')
    """
    try:
        if year and driver:
            url = f"{JOLPICA}/{year}/drivers/{driver}/results.json?limit=50"
        elif year:
            url = f"{JOLPICA}/{year}/results.json?limit=30"
        elif driver:
            url = f"{JOLPICA}/drivers/{driver}/results.json?limit=30"
        else:
            url = f"{JOLPICA}/current/results.json?limit=30"
        races = requests.get(url, timeout=10).json().get("MRData", {}).get("RaceTable", {}).get("Races", [])
        if not races:
            return "No results found"
        result = f"=== Historical Results ({len(races)} races) ===\n\n"
        for r in races[:20]:
            result += f"{r['season']} {r['raceName']}\n"
            for res in r.get("Results", [])[:10]:
                d = res["Driver"]
                c = res.get("Constructor", {})
                result += f"  P{res['position']:>2} {d.get('givenName','')} {d.get('familyName','')} ({c.get('name','?')})\n"
            result += "\n"
        return result
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_championship_standings(year: int = 0, standings_type: str = "driver") -> str:
    """Get championship standings from 1950 to present.

    Args:
        year: Season year (0 = current)
        standings_type: 'driver' or 'constructor'
    """
    try:
        season = str(year) if year else "current"
        if standings_type.lower() == "constructor":
            url = f"{JOLPICA}/{season}/constructorStandings.json"
        else:
            url = f"{JOLPICA}/{season}/driverStandings.json"
        lists = requests.get(url, timeout=10).json().get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
        if not lists:
            return "No standings found"
        s = lists[0]
        yr = s.get("season", "?")
        if standings_type.lower() == "constructor":
            entries = s.get("ConstructorStandings", [])
            result = f"=== {yr} Constructor Championship ===\n\n"
            for e in entries:
                c = e.get("Constructor", {})
                pos = e.get("position", e.get("positionText", "?"))
                result += f"P{pos:>2} {c.get('name','?'):25s} Pts: {e.get('points','0'):>6} Wins: {e.get('wins','0')}\n"
        else:
            entries = s.get("DriverStandings", [])
            result = f"=== {yr} Driver Championship ===\n\n"
            for e in entries:
                d = e.get("Driver", {})
                c = e.get("Constructors", [{}])[0] if e.get("Constructors") else {}
                name = f"{d.get('givenName','')} {d.get('familyName','')}"
                pos = e.get("position", e.get("positionText", "?"))
                result += f"P{pos:>2} {name:25s} ({c.get('name','?'):15s}) Pts: {e.get('points','0'):>6} Wins: {e.get('wins','0')}\n"
        return result
    except Exception as e:
        return f"Error: {e}"



# Common GP name → Ergast circuitId mapping for Jolpica/Ergast API lookups
CIRCUIT_NAME_MAP = {
    'australia': 'albert_park', 'melbourne': 'albert_park', 'albert park': 'albert_park',
    'bahrain': 'bahrain', 'sakhir': 'bahrain',
    'saudi': 'jeddah', 'saudi arabia': 'jeddah', 'jeddah': 'jeddah',
    'china': 'shanghai', 'shanghai': 'shanghai', 'chinese': 'shanghai',
    'japan': 'suzuka', 'suzuka': 'suzuka', 'japanese': 'suzuka',
    'miami': 'miami',
    'emilia romagna': 'imola', 'imola': 'imola',
    'monaco': 'monaco', 'monte carlo': 'monaco',
    'canada': 'villeneuve', 'montreal': 'villeneuve',
    'spain': 'catalunya', 'barcelona': 'catalunya', 'spanish': 'catalunya',
    'austria': 'red_bull_ring', 'spielberg': 'red_bull_ring',
    'britain': 'silverstone', 'silverstone': 'silverstone', 'british': 'silverstone',
    'hungary': 'hungaroring', 'hungaroring': 'hungaroring', 'budapest': 'hungaroring',
    'belgium': 'spa', 'spa': 'spa',
    'netherlands': 'zandvoort', 'zandvoort': 'zandvoort', 'dutch': 'zandvoort',
    'italy': 'monza', 'monza': 'monza', 'italian': 'monza',
    'azerbaijan': 'baku', 'baku': 'baku',
    'singapore': 'marina_bay', 'marina bay': 'marina_bay',
    'united states': 'americas', 'usa': 'americas', 'austin': 'americas', 'cota': 'americas',
    'mexico': 'rodriguez', 'mexico city': 'rodriguez',
    'brazil': 'interlagos', 'sao paulo': 'interlagos', 'interlagos': 'interlagos',
    'las vegas': 'vegas', 'vegas': 'vegas',
    'qatar': 'losail', 'losail': 'losail',
    'abu dhabi': 'yas_marina', 'yas marina': 'yas_marina',
    'portugal': 'portimao', 'portimao': 'portimao',
    'turkey': 'istanbul', 'istanbul': 'istanbul',
}


def _resolve_circuit_id(gp: str):
    """Resolve a GP name/country to an Ergast circuit ID."""
    return CIRCUIT_NAME_MAP.get(gp.lower().strip())


# =============================================================================
# FASTF1 TOOLS — Only registered if FastF1 is installed
# =============================================================================


if FASTF1_AVAILABLE:

    def _fig_to_image(fig) -> ImageContent:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)
        return ImageContent(type="image", data=base64.b64encode(buf.read()).decode(), mimeType="image/png")

    def _error_image(e) -> ImageContent:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(0.5, 0.5, f"Error: {e}", ha="center", va="center", fontsize=12, color="red")
        ax.axis("off")
        return _fig_to_image(fig)

    # ==============================================================================
    # MODULE 1: CALENDAR & SESSIONS
    # ==============================================================================
    
    @mcp.tool()
    def get_schedule(year: int) -> str:
        """Get the full race calendar for a specific year (excluding testing)."""
        try:
            schedule = fastf1.get_event_schedule(year)
            races = schedule[schedule['EventFormat'] != 'testing']
            return races[['RoundNumber', 'EventDate', 'Country', 'Location', 'EventName']].to_string(index=False)
        except Exception as e:
            return f"Error fetching schedule: {e}"
    
    @mcp.tool()
    def get_session_info(year: int, gp: str, session: str = 'R') -> str:
        """Get start time and status of a specific session (R=Race, Q=Quali, FP1, etc)."""
        try:
            s = fastf1.get_session(year, gp, session)
            return f"Session: {s.name}\nDate: {s.date}\nCircuit: {s.event.Location}\nStatus: {s.event.EventName}"
        except Exception as e:
            return f"Error: {e}"
    
    # ==============================================================================
    # MODULE 2: RACE RESULTS & LAPS
    # ==============================================================================
    
    @mcp.tool()
    def get_race_results(year: int, gp: str) -> str:
        """Get the final classification (Position, Driver, Team, Points)."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load(telemetry=False, weather=False)
            res = session.results[['ClassifiedPosition', 'Abbreviation', 'TeamName', 'Time', 'Points']]
            return res.to_string(index=False)
        except Exception as e:
            return f"Error: {e}"
    
    @mcp.tool()
    def get_fastest_lap_data(year: int, gp: str, driver: str, session: str = 'Q') -> str:
        """Get detailed stats for a driver's fastest lap (Sector times, Speed trap)."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            lap = s.laps.pick_driver(driver).pick_fastest()

            if lap is None or pd.isna(lap['LapTime']):
                return f"No qualifying lap found for {driver} in {gp} {year} {session}. The driver may have been eliminated in an earlier session."

            return f"""
            🚗 Driver: {driver}
            ⏱️ Time: {str(lap['LapTime']).split('days')[-1]}
            🟣 Sector 1: {lap['Sector1Time'].total_seconds()}s
            🟣 Sector 2: {lap['Sector2Time'].total_seconds()}s
            🟣 Sector 3: {lap['Sector3Time'].total_seconds()}s
            🚀 Speed Trap: {lap['SpeedST']} km/h
            🛞 Tyre: {lap['Compound']} ({lap['TyreLife']} laps old)
            """
        except Exception as e:
            return f"Error: {e}"
    
    # ==============================================================================
    # MODULE 3: TELEMETRY & PHYSICS (VISUAL)
    # ==============================================================================
    
    @mcp.tool()
    def plot_telemetry_comparison(year: int, gp: str, driver1: str, driver2: str, session: str = 'Q') -> ImageContent:
        """
        Generates a Speed Trace comparison image between two drivers.
        Returns: An ImageContent object that can be displayed in the client.
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
    
            d1 = s.laps.pick_driver(driver1).pick_fastest()
            d2 = s.laps.pick_driver(driver2).pick_fastest()
            
            t1 = d1.get_car_data().add_distance()
            t2 = d2.get_car_data().add_distance()
    
            fastf1.plotting.setup_mpl()
            fig, ax = plt.subplots(figsize=(10, 5))
            
            ax.plot(t1['Distance'], t1['Speed'], color='blue', label=driver1)
            ax.plot(t2['Distance'], t2['Speed'], color='orange', label=driver2)
            ax.set_ylabel('Speed (km/h)')
            ax.set_xlabel('Distance (m)')
            ax.legend()
            plt.title(f"{driver1} vs {driver2} - Speed Trace")
    
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close(fig)
            return ImageContent(type="image", data=img_base64, mimeType="image/png")
        except Exception as e:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.text(0.5, 0.5, f"Error: {str(e)}", 
                    ha='center', va='center', fontsize=12, color='red')
            ax.axis('off')
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close(fig)
            
            return ImageContent(type="image",data=img_base64, mimeType="image/png")
            #return f"Plot Error: {e}"
    
    @mcp.tool()
    def plot_multi_telemetry_comparison(year: int, gp: str, driver: str, lap1: int, lap2: int, session: str = 'R') -> ImageContent:
        """
        Compare full telemetry (speed, throttle, brake, gear) between two laps for same driver.
        Example: Compare first lap vs last lap for Piastri Brazil 2024
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            driver_laps = s.laps.pick_drivers(driver)
            
            # Get specific laps
            lap1_data = driver_laps[driver_laps['LapNumber'] == lap1].iloc[0]
            lap2_data = driver_laps[driver_laps['LapNumber'] == lap2].iloc[0]
            
            tel1 = lap1_data.get_telemetry().add_distance()
            tel2 = lap2_data.get_telemetry().add_distance()
            
            fastf1.plotting.setup_mpl()
            
            # Create 4 subplots
            fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True)
            
            # Speed
            axes[0].plot(tel1['Distance'], tel1['Speed'], label=f'Lap {lap1}', color='blue')
            axes[0].plot(tel2['Distance'], tel2['Speed'], label=f'Lap {lap2}', color='red')
            axes[0].set_ylabel('Speed (km/h)')
            axes[0].legend(loc='upper right')
            axes[0].grid(True)
            
            # Throttle
            axes[1].plot(tel1['Distance'], tel1['Throttle'], label=f'Lap {lap1}', color='blue')
            axes[1].plot(tel2['Distance'], tel2['Throttle'], label=f'Lap {lap2}', color='red')
            axes[1].set_ylabel('Throttle (%)')
            axes[1].legend(loc='upper right')
            axes[1].grid(True)
            
            # Brake
            axes[2].plot(tel1['Distance'], tel1['Brake'], label=f'Lap {lap1}', color='blue')
            axes[2].plot(tel2['Distance'], tel2['Brake'], label=f'Lap {lap2}', color='red')
            axes[2].set_ylabel('Brake')
            axes[2].legend(loc='upper right')
            axes[2].grid(True)
            
            # Gear
            axes[3].plot(tel1['Distance'], tel1['nGear'], label=f'Lap {lap1}', color='blue')
            axes[3].plot(tel2['Distance'], tel2['nGear'], label=f'Lap {lap2}', color='red')
            axes[3].set_ylabel('Gear')
            axes[3].set_xlabel('Distance (m)')
            axes[3].legend(loc='upper right')
            axes[3].grid(True)
            
            plt.suptitle(f"{driver} - {gp} {year} - Lap {lap1} vs Lap {lap2}", fontsize=14, fontweight='bold')
            plt.tight_layout()
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close(fig)
            
            return ImageContent(type="image", data=img_base64, mimeType="image/png")
        except Exception as e:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.text(0.5, 0.5, f"Error: {str(e)}", 
                    ha='center', va='center', fontsize=12, color='red')
            ax.axis('off')
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close(fig)
            
            return ImageContent(type="image", data=img_base64, mimeType="image/png")
    
    @mcp.tool()
    def plot_driver_telemetry_comparison(year: int, gp: str, driver1: str, driver2: str, lap_number: int, session: str = "R") -> ImageContent:
        """Plot comprehensive telemetry comparison (speed, throttle, brake, gear) between two drivers for the same lap.
        
        Args:
            year: Season year
            gp: Grand Prix name
            driver1: First driver identifier (3-letter code)
            driver2: Second driver identifier (3-letter code)
            lap_number: Lap number to compare
            session: Session type (R=Race, Q=Qualifying, FP1/FP2/FP3=Practice, S=Sprint)
        
        Returns:
            ImageContent with 4 subplots showing speed, throttle, brake, and gear data for both drivers
        """
        try:
            fastf1.Cache.enable_cache('cache')
            session_obj = fastf1.get_session(year, gp, session)
            session_obj.load()
            
            # Get laps for both drivers
            driver1_lap = session_obj.laps.pick_drivers(driver1).pick_laps(lap_number).iloc[0]
            driver2_lap = session_obj.laps.pick_drivers(driver2).pick_laps(lap_number).iloc[0]
            
            # Get telemetry data
            tel1 = driver1_lap.get_telemetry()
            tel2 = driver2_lap.get_telemetry()
            
            # Get driver info for labeling
            driver1_info = session_obj.get_driver(driver1)
            driver2_info = session_obj.get_driver(driver2)
            driver1_name = f"{driver1_info['FirstName']} {driver1_info['LastName']}"
            driver2_name = f"{driver2_info['FirstName']} {driver2_info['LastName']}"
            
            # Create figure with 4 subplots
            fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True)
            fig.suptitle(f"{year} {gp} - {session} - Lap {lap_number}\n{driver1_name} vs {driver2_name}", fontsize=14, fontweight='bold')
            
            # Plot Speed
            axes[0].plot(tel1['Distance'], tel1['Speed'], color='blue', label=driver1_name, linewidth=2)
            axes[0].plot(tel2['Distance'], tel2['Speed'], color='red', label=driver2_name, linewidth=2)
            axes[0].set_ylabel('Speed (km/h)', fontsize=10)
            axes[0].legend(loc='upper right')
            axes[0].grid(True, alpha=0.3)
            
            # Plot Throttle
            axes[1].plot(tel1['Distance'], tel1['Throttle'], color='blue', label=driver1_name, linewidth=2)
            axes[1].plot(tel2['Distance'], tel2['Throttle'], color='red', label=driver2_name, linewidth=2)
            axes[1].set_ylabel('Throttle (%)', fontsize=10)
            axes[1].legend(loc='upper right')
            axes[1].grid(True, alpha=0.3)
            
            # Plot Brake
            axes[2].plot(tel1['Distance'], tel1['Brake'], color='blue', label=driver1_name, linewidth=2)
            axes[2].plot(tel2['Distance'], tel2['Brake'], color='red', label=driver2_name, linewidth=2)
            axes[2].set_ylabel('Brake', fontsize=10)
            axes[2].legend(loc='upper right')
            axes[2].grid(True, alpha=0.3)
            
            # Plot Gear
            axes[3].plot(tel1['Distance'], tel1['nGear'], color='blue', label=driver1_name, linewidth=2)
            axes[3].plot(tel2['Distance'], tel2['nGear'], color='red', label=driver2_name, linewidth=2)
            axes[3].set_ylabel('Gear', fontsize=10)
            axes[3].set_xlabel('Distance (m)', fontsize=10)
            axes[3].legend(loc='upper right')
            axes[3].grid(True, alpha=0.3)
            
            plt.tight_layout()
            
            # Save to buffer
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            plt.close()
            
            # Encode to base64
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            
            return ImageContent(
                type="image",
                data=img_base64,
                mimeType="image/png"
            )
        except Exception as e:
            return _error_image(e)
    
    @mcp.tool()
    def plot_gear_shifts(year: int, gp: str, driver: str, session: str = 'Q') -> ImageContent:
        """Generates a Gear Shift chart for a single driver."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            # Get the driver's laps
            driver_laps = s.laps.pick_driver(driver)
            if driver_laps.empty:
                raise ValueError(f"No laps found for driver {driver}")
            
            # Pick the fastest lap
            lap = driver_laps.pick_fastest()
            if lap is None or lap.empty:
                raise ValueError(f"No valid fastest lap found for {driver}")
            
            # Get telemetry data
            tel = lap.get_telemetry().add_distance()
            if tel is None or tel.empty:
                raise ValueError(f"No telemetry data available for {driver}")
    
            fastf1.plotting.setup_mpl()
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(tel['Distance'], tel['nGear'], label='Gear', color='green')
            ax.set_ylabel('Gear')
            ax.set_xlabel('Distance (m)')
            plt.title(f"{driver} Gear Usage - {gp} {year}")
            ax.legend()
    
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close(fig)
            
            return ImageContent(
                type="image",
                data=img_base64,
                mimeType="image/png"
            )
        except Exception as e:
            # Create an error image instead of returning a string
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.text(0.5, 0.5, f"Error: {str(e)}", 
                    ha='center', va='center', fontsize=12, color='red', wrap=True)
            ax.axis('off')
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close(fig)
            
            return ImageContent(
                type="image",
                data=img_base64,
                mimeType="image/png"
            )
    
    # ==============================================================================
    # MODULE 4: WEATHER & TRACK CONDITIONS
    # ==============================================================================
    
    @mcp.tool()
    def get_weather_data(year: int, gp: str, session: str = 'R') -> str:
        """Get detailed weather conditions (Rain, Track Temp, Wind)."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(laps=False, weather=True)
            w = s.weather_data
            
            return f"""
            🌡️ Average Air Temp: {w['AirTemp'].mean():.1f}°C
            🔥 Average Track Temp: {w['TrackTemp'].mean():.1f}°C
            💧 Humidity: {w['Humidity'].mean():.1f}%
            🌧️ Rain Detected: {w['Rainfall'].any()}
            💨 Wind Speed: {w['WindSpeed'].mean():.1f} m/s
            """
        except Exception as e:
            return f"Weather Error: {e}"
    
    @mcp.tool()
    def get_circuit_info(year: int, gp: str) -> str:
        """Get track layout info (Corners, DRS Zones)."""
        try:
            s = fastf1.get_session(year, gp, 'Q')
            s.load(laps=True, telemetry=True) # Telemetry needed for circuit info
            info = s.get_circuit_info()
            
            corners = info.corners[['Number', 'Letter', 'Angle', 'Distance']].to_string(index=False)
            return f"Circuit Rotation: {info.rotation} degrees\n\nCorners:\n{corners}"
        except Exception as e:
            return f"Circuit Info Error: {e}"
    
    # ==============================================================================
    # MODULE 5: TYRE STRATEGY
    # ==============================================================================
    
    @mcp.tool()
    def get_driver_tyre_detail(year: int, gp: str, driver: str) -> str:
        """Get detailed tyre stint data for a specific driver via FastF1 — compound, laps, and degradation."""
        try:
            s = fastf1.get_session(year, gp, 'R')
            s.load()
            laps = s.laps.pick_driver(driver)
            
            stints = laps.groupby('Stint').agg({
                'Compound': 'first',
                'LapNumber': ['min', 'max'],
                'TyreLife': 'max'
            })
            return f"Tyre Strategy for {driver}:\n{stints.to_string()}"
        except Exception as e:
            return f"Strategy Error: {e}"
    
    # ==============================================================================
    # ==============================================================================
    # MODULE 7: PIT STOPS & STRATEGY
    # ==============================================================================
    
    @mcp.tool()
    def get_pit_stop_detail(year: int, gp: str, driver: str = None) -> str:
        """Get detailed pit stop data via FastF1 — includes tyre compounds swapped and duration."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            if driver:
                laps = session.laps.pick_drivers(driver)
            else:
                laps = session.laps
            
            # Get pit laps (laps where pit stop occurred)
            pit_laps = laps[laps['PitInTime'].notna()]

            if len(pit_laps) == 0:
                return "No pit stops found"

            # Build a map of PitOutTime per driver per lap for cross-lap lookups
            all_session_laps = session.laps
            result = "🔧 Pit Stops:\n"
            for idx, lap in pit_laps.iterrows():
                duration = None
                # Try 1: Use PitStopDuration if available
                if 'PitStopDuration' in lap.index and pd.notna(lap.get('PitStopDuration')):
                    dur_val = lap['PitStopDuration']
                    duration = dur_val.total_seconds() if hasattr(dur_val, 'total_seconds') else float(dur_val)
                # Try 2: PitOutTime on same row
                if (duration is None or pd.isna(duration)) and pd.notna(lap.get('PitOutTime')) and pd.notna(lap['PitInTime']):
                    duration = (lap['PitOutTime'] - lap['PitInTime']).total_seconds()
                # Try 3: PitOutTime on the next lap for this driver
                if duration is None or pd.isna(duration):
                    drv_laps = all_session_laps[all_session_laps['Driver'] == lap['Driver']].sort_values('LapNumber')
                    next_laps = drv_laps[drv_laps['LapNumber'] > lap['LapNumber']]
                    if len(next_laps) > 0:
                        nxt = next_laps.iloc[0]
                        if pd.notna(nxt.get('PitOutTime')):
                            duration = (nxt['PitOutTime'] - lap['PitInTime']).total_seconds()
                dur_str = f"{duration:.1f}s" if duration is not None and not pd.isna(duration) else "N/A"
                result += f"Lap {int(lap['LapNumber'])}: {lap['Driver']} - {dur_str} ({lap['Compound']})\n"

            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 8: STANDINGS & POINTS
    # ==============================================================================
    
    @mcp.tool()
    def get_driver_standings(year: int, round_number: int = None) -> str:
        """Get driver championship standings after a specific round or latest."""
        try:
            if round_number:
                url = f"https://api.jolpi.ca/ergast/f1/{year}/{round_number}/driverStandings.json"
            else:
                url = f"https://api.jolpi.ca/ergast/f1/{year}/driverStandings.json"
            
            response = requests.get(url, timeout=15)
            data = response.json()
            standings = data['MRData']['StandingsTable']['StandingsLists'][0]['DriverStandings']
            
            result = f"🏆 Driver Standings {year}"
            if round_number:
                result += f" (After Round {round_number})"
            result += ":\n\n"
            
            for driver in standings:
                pos = driver['position']
                name = f"{driver['Driver']['givenName']} {driver['Driver']['familyName']}"
                points = driver['points']
                team = driver['Constructors'][0]['name']
                result += f"{pos}. {name} ({team}) - {points} pts\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    @mcp.tool()
    def get_constructor_standings(year: int, round_number: int = None) -> str:
        """Get constructor/team championship standings."""
        try:
            if round_number:
                url = f"https://api.jolpi.ca/ergast/f1/{year}/{round_number}/constructorStandings.json"
            else:
                url = f"https://api.jolpi.ca/ergast/f1/{year}/constructorStandings.json"
            
            response = requests.get(url, timeout=15)
            data = response.json()
            standings = data['MRData']['StandingsTable']['StandingsLists'][0]['ConstructorStandings']
            
            result = f"🏆 Constructor Standings {year}"
            if round_number:
                result += f" (After Round {round_number})"
            result += ":\n\n"
            
            for team in standings:
                pos = team['position']
                name = team['Constructor']['name']
                points = team['points']
                result += f"{pos}. {name} - {points} pts\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 9: SPRINT RACES
    # ==============================================================================
    
    @mcp.tool()
    def get_sprint_results(year: int, gp: str) -> str:
        """Get sprint race results (for sprint weekends)."""
        try:
            session = fastf1.get_session(year, gp, 'S')
            session.load(telemetry=False, weather=False)
            
            res = session.results[['ClassifiedPosition', 'Abbreviation', 'TeamName', 'Time', 'Points']]
            return f"🏁 Sprint Results - {gp} {year}:\n{res.to_string(index=False)}"
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 10: SECTOR & LAP ANALYSIS
    # ==============================================================================
    
    @mcp.tool()
    def compare_sector_times(year: int, gp: str, driver1: str, driver2: str, session: str = 'Q') -> str:
        """Compare sector times between two drivers."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            lap1 = s.laps.pick_drivers(driver1).pick_fastest()
            lap2 = s.laps.pick_drivers(driver2).pick_fastest()

            if lap1 is None or (hasattr(lap1, 'empty') and lap1.empty):
                return f"No lap data for {driver1} in {session} session"
            if lap2 is None or (hasattr(lap2, 'empty') and lap2.empty):
                return f"No lap data for {driver2} in {session} session"

            result = f"⏱️ Sector Comparison - {driver1} vs {driver2}:\n\n"

            sectors = ['Sector1Time', 'Sector2Time', 'Sector3Time']
            for i, sector in enumerate(sectors, 1):
                time1 = lap1[sector].total_seconds()
                time2 = lap2[sector].total_seconds()
                diff = time1 - time2
                faster = driver1 if diff < 0 else driver2
                result += f"Sector {i}: {time1:.3f}s vs {time2:.3f}s (Δ {abs(diff):.3f}s, {faster} faster)\n"
            
            total1 = lap1['LapTime'].total_seconds()
            total2 = lap2['LapTime'].total_seconds()
            diff_total = total1 - total2
            faster_total = driver1 if diff_total < 0 else driver2
            result += f"\nTotal: {total1:.3f}s vs {total2:.3f}s (Δ {abs(diff_total):.3f}s, {faster_total} faster)"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    @mcp.tool()
    def get_lap_times_fastf1(year: int, gp: str, driver: str, session: str = 'R') -> str:
        """Get all lap times for a driver via FastF1 — includes compound and tyre life per lap."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            laps = s.laps.pick_drivers(driver)
            
            result = f"⏱️ Lap Times - {driver} ({gp} {year} {session}):\n\n"
            for idx, lap in laps.iterrows():
                lap_num = int(lap['LapNumber'])
                lap_time = str(lap['LapTime']).split('days')[-1].strip() if pd.notna(lap['LapTime']) else 'N/A'
                compound = lap['Compound'] if pd.notna(lap['Compound']) else 'N/A'
                deleted = " [DELETED]" if lap.get('Deleted', False) else ""
                result += f"Lap {lap_num}: {lap_time} ({compound}){deleted}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 11: DELETED LAPS & TRACK LIMITS
    # ==============================================================================
    
    @mcp.tool()
    def get_deleted_laps(year: int, gp: str, session: str = 'Q') -> str:
        """Get all laps deleted due to track limits or other violations."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(messages=True)
            
            deleted_laps = s.laps[s.laps['Deleted'] == True]
            
            if len(deleted_laps) == 0:
                return "No deleted laps found"
            
            result = f"🚫 Deleted Laps - {gp} {year} {session}:\n\n"
            for idx, lap in deleted_laps.iterrows():
                driver = lap['Driver']
                lap_num = int(lap['LapNumber'])
                lap_time = str(lap['LapTime']).split('days')[-1].strip()
                reason = lap['DeletedReason'] if pd.notna(lap['DeletedReason']) else 'Unknown'
                result += f"{driver} - Lap {lap_num} ({lap_time}): {reason}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 12: RACE PACE & POSITION CHANGES
    # ==============================================================================
    
    @mcp.tool()
    def get_position_changes(year: int, gp: str, driver: str) -> str:
        """Track position changes throughout the race for a driver."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            laps = session.laps.pick_drivers(driver)
            
            # Get actual grid position from session results
            driver_result = session.results[session.results['Abbreviation'] == driver]
            if not driver_result.empty and pd.notna(driver_result.iloc[0]['GridPosition']):
                start_pos = int(driver_result.iloc[0]['GridPosition'])
            else:
                start_pos = int(laps.iloc[0]['Position'])
            
            result = f"📊 Position Changes - {driver} ({gp} {year}):\n\n"
            result += f"Starting Position: P{start_pos}\n"
            result += f"Finishing Position: P{int(laps.iloc[-1]['Position'])}\n\n"
            
            result += "Lap-by-lap positions:\n"
            for idx, lap in laps.iterrows():
                if pd.notna(lap['Position']):
                    result += f"Lap {int(lap['LapNumber'])}: P{int(lap['Position'])}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 13: TRACK STATUS & RACE CONTROL
    # ==============================================================================
    
    @mcp.tool()
    def get_track_status(year: int, gp: str, session: str = 'R') -> str:
        """Get track status changes (yellow flags, safety car, red flag, etc.)."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            track_status = s.track_status
            
            result = f"🚦 Track Status Changes - {gp} {year} {session}:\n\n"
            
            # Track status meanings
            status_map = {
                '1': '🟢 All Clear',
                '2': '🟡 Yellow Flag',
                '3': '🟢 Green Flag',
                '4': '🔴 Safety Car',
                '5': '🔴 Red Flag',
                '6': '🟡 Virtual Safety Car',
                '7': '🟢 VSC Ending'
            }
            
            for idx, row in track_status.iterrows():
                status = row['Status']
                time = row['Time']
                status_text = status_map.get(status, f'Status {status}')
                result += f"{time}: {status_text}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    @mcp.tool()
    def get_race_control_messages(year: int, gp: str, session: str = 'R') -> str:
        """Get all race control messages during a session."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(messages=True)
            
            messages = s.race_control_messages
            
            if len(messages) == 0:
                return "No race control messages found"
            
            result = f"📢 Race Control Messages - {gp} {year} {session}:\n\n"
            
            for idx, msg in messages.iterrows():
                time = msg['Time']
                category = msg['Category']
                message = msg['Message']
                result += f"[{time}] {category}: {message}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 14: DRIVER & TEAM INFO
    # ==============================================================================
    
    @mcp.tool()
    def get_driver_info(year: int, gp: str, driver: str) -> str:
        """Get detailed driver information (number, team, headshot, etc.)."""
        try:
            DRIVER_COUNTRIES = {
                'VER': 'NED', 'HAM': 'GBR', 'NOR': 'GBR', 'LEC': 'MON',
                'ANT': 'ITA', 'RUS': 'GBR', 'PIA': 'AUS', 'BEA': 'GBR',
                'GAS': 'FRA', 'LAW': 'NZL', 'HAD': 'FRA', 'SAI': 'ESP',
                'ALO': 'ESP', 'OCO': 'FRA', 'BOT': 'FIN', 'ALB': 'THA',
                'HUL': 'GER', 'STR': 'CAN', 'COL': 'ARG', 'LIN': 'GBR',
                'PER': 'MEX',
            }

            session = fastf1.get_session(year, gp, 'R')
            session.load(telemetry=False)

            driver_result = session.get_driver(driver)

            country = driver_result.get('CountryCode', '')
            if not country or pd.isna(country):
                abbr = driver_result.get('Abbreviation', driver)
                country = DRIVER_COUNTRIES.get(abbr, 'N/A')

            result = f"👤 Driver Info - {driver}:\n\n"
            result += f"Full Name: {driver_result['FullName']}\n"
            result += f"Number: {driver_result['DriverNumber']}\n"
            result += f"Team: {driver_result['TeamName']}\n"
            result += f"Country: {country}\n"
            result += f"Abbreviation: {driver_result['Abbreviation']}\n"

            if pd.notna(driver_result.get('HeadshotUrl')):
                result += f"Headshot: {driver_result['HeadshotUrl']}\n"

            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    @mcp.tool()
    def get_team_laps(year: int, gp: str, team: str, session: str = 'R') -> str:
        """Get all laps for a specific team (both drivers)."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            team_laps = s.laps.pick_teams(team)
            
            drivers = pd.unique(team_laps['Driver'])
            
            result = f"🏎️ Team Laps - {team} ({gp} {year} {session}):\n\n"
            
            for driver in drivers:
                driver_laps = team_laps[team_laps['Driver'] == driver]
                fastest = driver_laps.pick_fastest()
                avg_time = driver_laps['LapTime'].mean()
                
                result += f"{driver}:\n"
                result += f"  Laps: {len(driver_laps)}\n"
                result += f"  Fastest: {str(fastest['LapTime']).split('days')[-1].strip()}\n"
                result += f"  Average: {str(avg_time).split('days')[-1].strip()}\n\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 15: SPEED TRAP & DRS
    # ==============================================================================
    
    @mcp.tool()
    def get_speed_trap_comparison(year: int, gp: str, session: str = 'Q') -> str:
        """Compare speed trap data across all drivers."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            # Get fastest lap per driver
            drivers = pd.unique(s.laps['Driver'])
            speed_data = []
            
            for drv in drivers:
                fastest = s.laps.pick_drivers(drv).pick_fastest()
                if fastest is not None and pd.notna(fastest['SpeedST']):
                    speed_data.append({
                        'Driver': drv,
                        'Team': fastest['Team'],
                        'Speed': fastest['SpeedST']
                    })
            
            # Sort by speed
            speed_df = pd.DataFrame(speed_data).sort_values('Speed', ascending=False)
            
            result = f"🚀 Speed Trap Comparison - {gp} {year} {session}:\n\n"
            for i, row in speed_df.iterrows():
                result += f"{row['Driver']} ({row['Team']}): {row['Speed']:.1f} km/h\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    @mcp.tool()
    def analyze_drs_usage(year: int, gp: str, driver: str, session: str = 'R') -> str:
        """Analyze DRS usage patterns for a driver's fastest lap."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            lap = s.laps.pick_drivers(driver).pick_fastest()
            tel = lap.get_car_data()
            
            # Count DRS activations (DRS values: 0=Off, 1-14=DRS levels)
            drs_active = tel[tel['DRS'] > 0]
            drs_percentage = (len(drs_active) / len(tel)) * 100 if len(tel) > 0 else 0
            
            result = f"💨 DRS Analysis - {driver} ({gp} {year}):\n\n"
            result += f"DRS Active: {drs_percentage:.1f}% of lap\n"
            result += f"DRS Samples: {len(drs_active)} / {len(tel)}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 16: COMPOUND & TIRE ANALYSIS
    # ==============================================================================
    
    @mcp.tool()
    def compare_tire_compounds(year: int, gp: str, session: str = 'R') -> str:
        """Compare average lap times across different tire compounds."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            # Filter for accurate laps only
            laps = s.laps.pick_accurate()
            
            compounds = pd.unique(laps['Compound'])
            
            result = f"🛞 Tire Compound Comparison - {gp} {year}:\n\n"
            
            for compound in compounds:
                if pd.notna(compound):
                    compound_laps = laps.pick_compounds(compound)
                    avg_time = compound_laps['LapTime'].mean()
                    fastest_time = compound_laps['LapTime'].min()
                    result += f"{compound}:\n"
                    result += f"  Average: {str(avg_time).split('days')[-1].strip()}\n"
                    result += f"  Fastest: {str(fastest_time).split('days')[-1].strip()}\n"
                    result += f"  Laps: {len(compound_laps)}\n\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    @mcp.tool()
    def get_stint_analysis(year: int, gp: str, driver: str) -> str:
        """Analyze each stint: compound, lap times, degradation."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            laps = session.laps.pick_drivers(driver)
            
            result = f"📊 Stint Analysis - {driver} ({gp} {year}):\n\n"
            
            stints = laps.groupby('Stint')
            
            for stint_num, stint_laps in stints:
                compound = stint_laps.iloc[0]['Compound']
                start_lap = int(stint_laps.iloc[0]['LapNumber'])
                end_lap = int(stint_laps.iloc[-1]['LapNumber'])
                num_laps = len(stint_laps)
                
                # Calculate average pace
                avg_time = stint_laps['LapTime'].mean()
                
                result += f"Stint {int(stint_num)}: {compound}\n"
                result += f"  Laps {start_lap}-{end_lap} ({num_laps} laps)\n"
                result += f"  Avg Time: {str(avg_time).split('days')[-1].strip()}\n\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 17: DNF & RETIREMENTS
    # ==============================================================================
    
    @mcp.tool()
    def get_dnf_list(year: int, gp: str) -> str:
        """Get list of drivers who did not finish the race and reasons."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load(telemetry=False)
            
            results = session.results
            
            # Filter for actual retirements/DNFs (not lapped finishers)
            retirement_statuses = ['Retired', 'DNF', 'DNS', 'DSQ', 'Accident', 'Collision',
                                   'Engine', 'Gearbox', 'Hydraulics', 'Brakes', 'Mechanical',
                                   'Electrical', 'Spun off', 'Damage', 'Withdrew', 'Disqualified']
            dnfs = results[results['Status'].str.contains('|'.join(retirement_statuses), case=False, na=False)]
            
            if len(dnfs) == 0:
                return "All drivers finished the race!"
            
            result = f"⚠️ DNF/Retirements - {gp} {year}:\n\n"
            
            for idx, driver in dnfs.iterrows():
                name = driver['Abbreviation']
                team = driver['TeamName']
                status = driver['Status']
                result += f"{name} ({team}): {status}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 18: FASTEST SECTORS
    # ==============================================================================
    
    @mcp.tool()
    def get_fastest_sectors(year: int, gp: str, session: str = 'Q') -> str:
        """Find who set the fastest time in each sector."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            drivers = pd.unique(s.laps['Driver'])
            
            # Get fastest lap per driver
            fastest_laps = []
            for drv in drivers:
                lap = s.laps.pick_drivers(drv).pick_fastest()
                if lap is not None:
                    fastest_laps.append(lap)

            if not fastest_laps:
                return f"No valid lap data found for {gp} {year} {session}."

            all_fastest = pd.DataFrame(fastest_laps)

            result = f"⚡ Fastest Sectors - {gp} {year} {session}:\n\n"

            for i in [1, 2, 3]:
                sector_col = f'Sector{i}Time'
                if sector_col not in all_fastest.columns:
                    result += f"Sector {i}: No data available\n"
                    continue
                valid = all_fastest[all_fastest[sector_col].notna()]
                if valid.empty:
                    result += f"Sector {i}: No data available\n"
                    continue
                fastest_sector = valid.sort_values(sector_col).iloc[0]
                driver = fastest_sector['Driver']
                time = fastest_sector[sector_col].total_seconds()
                result += f"Sector {i}: {driver} - {time:.3f}s\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 19: GRID VS FINISH
    # ==============================================================================
    
    @mcp.tool()
    def compare_grid_to_finish(year: int, gp: str) -> str:
        """Compare starting grid positions to finishing positions."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load(telemetry=False)
            
            results = session.results
            
            result = f"🏁 Grid vs Finish - {gp} {year}:\n\n"
            
            for idx, driver in results.iterrows():
                name = driver['Abbreviation']
                grid = int(driver['GridPosition']) if pd.notna(driver['GridPosition']) else 'N/A'
                finish = driver['ClassifiedPosition']
                
                if grid != 'N/A' and finish != 'R':
                    try:
                        finish_int = int(finish)
                        change = grid - finish_int
                        change_str = f"+{change}" if change > 0 else str(change)
                        result += f"{name}: P{grid} → P{finish} ({change_str})\n"
                    except (ValueError, TypeError):
                        result += f"{name}: P{grid} → {finish}\n"
                else:
                    result += f"{name}: P{grid} → {finish}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 20: QUALIFYING SESSIONS (Q1, Q2, Q3)
    # ==============================================================================
    
    @mcp.tool()
    def get_qualifying_progression(year: int, gp: str) -> str:
        """Show who was eliminated in Q1, Q2 and who made it to Q3."""
        try:
            session = fastf1.get_session(year, gp, 'Q')
            session.load()
            
            q1, q2, q3 = session.laps.split_qualifying_sessions()

            # Use session results for complete driver list (covers drivers with no lap times)
            all_drivers = set(session.results['Abbreviation'].dropna().tolist())
            q1_drivers = set(pd.unique(q1['Driver'])) if q1 is not None and len(q1) > 0 else set()
            q2_drivers = set(pd.unique(q2['Driver'])) if q2 is not None and len(q2) > 0 else set()
            q3_drivers = set(pd.unique(q3['Driver'])) if q3 is not None and len(q3) > 0 else set()

            # Drivers in results but missing from lap data belong to Q1 (they entered but may not have set a time)
            missing_from_laps = all_drivers - q1_drivers - q2_drivers - q3_drivers
            q1_drivers = q1_drivers | missing_from_laps

            result = f"🏁 Qualifying Progression - {gp} {year}:\n\n"

            # Q3 participants
            if q3_drivers:
                result += f"Q3 (Top 10): {', '.join(sorted(q3_drivers))}\n\n"

            # Q2 eliminated
            if q2_drivers:
                eliminated_q2 = sorted(q2_drivers - q3_drivers)
                if eliminated_q2:
                    result += f"Eliminated in Q2 (P11-15): {', '.join(eliminated_q2)}\n\n"

            # Q1 eliminated
            if q1_drivers:
                eliminated_q1 = sorted(q1_drivers - q2_drivers)
                if eliminated_q1:
                    result += f"Eliminated in Q1 (P16-20): {', '.join(eliminated_q1)}\n"

            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 21: LAP CONSISTENCY & STATISTICS
    # ==============================================================================
    
    @mcp.tool()
    def analyze_lap_consistency(year: int, gp: str, driver: str, session: str = 'R') -> str:
        """Analyze lap time consistency (standard deviation, variation)."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            laps = s.laps.pick_drivers(driver).pick_accurate()
            
            if len(laps) == 0:
                return "No accurate laps found for analysis"
            
            lap_times = laps['LapTime'].dt.total_seconds()
            
            avg = lap_times.mean()
            fastest = lap_times.min()
            slowest = lap_times.max()
            std_dev = lap_times.std()
            
            result = f"📊 Lap Consistency - {driver} ({gp} {year} {session}):\n\n"
            result += f"Average: {avg:.3f}s\n"
            result += f"Fastest: {fastest:.3f}s\n"
            result += f"Slowest: {slowest:.3f}s\n"
            result += f"Std Dev: {std_dev:.3f}s\n"
            result += f"Range: {slowest - fastest:.3f}s\n"
            result += f"Total Laps: {len(laps)}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 22: BRAKE & THROTTLE ANALYSIS
    # ==============================================================================
    
    @mcp.tool()
    def analyze_brake_points(year: int, gp: str, driver: str, session: str = 'Q') -> str:
        """Analyze braking patterns on fastest lap."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            lap = s.laps.pick_drivers(driver).pick_fastest()
            if lap is None or (hasattr(lap, 'empty') and lap.empty):
                return f"No lap data for {driver} in {session} session"
            tel = lap.get_car_data()

            # Brake is boolean: True when braking
            brake_points = tel[tel['Brake'] == True]
            total_brake_time = len(brake_points) / len(tel) * 100 if len(tel) > 0 else 0
            
            # Throttle percentage
            avg_throttle = tel['Throttle'].mean()
            max_throttle = tel['Throttle'].max()
            full_throttle = len(tel[tel['Throttle'] >= 99]) / len(tel) * 100 if len(tel) > 0 else 0
            
            result = f"🚦 Brake & Throttle Analysis - {driver} ({gp} {year}):\n\n"
            result += f"Braking: {total_brake_time:.1f}% of lap\n"
            result += f"Brake Events: {len(brake_points)} samples\n\n"
            result += f"Avg Throttle: {avg_throttle:.1f}%\n"
            result += f"Full Throttle: {full_throttle:.1f}% of lap\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 23: RPM & ENGINE ANALYSIS
    # ==============================================================================
    
    @mcp.tool()
    def analyze_rpm_data(year: int, gp: str, driver: str, session: str = 'Q') -> str:
        """Analyze engine RPM patterns on fastest lap."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            lap = s.laps.pick_drivers(driver).pick_fastest()
            if lap is None or (hasattr(lap, 'empty') and lap.empty):
                return f"No lap data for {driver} in {session} session"
            tel = lap.get_car_data()

            avg_rpm = tel['RPM'].mean()
            max_rpm = tel['RPM'].max()
            min_rpm = tel['RPM'].min()
            
            result = f"🔧 RPM Analysis - {driver} ({gp} {year}):\n\n"
            result += f"Average RPM: {avg_rpm:.0f}\n"
            result += f"Max RPM: {max_rpm:.0f}\n"
            result += f"Min RPM: {min_rpm:.0f}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 24: FASTEST PIT STOPS
    # ==============================================================================
    
    @mcp.tool()
    def get_fastest_pit_stops(year: int, gp: str, top_n: int = 10) -> str:
        """Get the fastest pit stops of the race."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            all_laps = session.laps
            pit_laps = all_laps[all_laps['PitInTime'].notna()].copy()

            if len(pit_laps) == 0:
                return "No pit stops found"

            # Calculate pit stop duration — try multiple approaches
            durations = []
            for idx, lap in pit_laps.iterrows():
                duration = None
                # Try 1: PitStopDuration column
                if 'PitStopDuration' in lap.index and pd.notna(lap.get('PitStopDuration')):
                    dur_val = lap['PitStopDuration']
                    duration = dur_val.total_seconds() if hasattr(dur_val, 'total_seconds') else float(dur_val)
                # Try 2: PitOutTime on same row
                if (duration is None or pd.isna(duration)) and pd.notna(lap.get('PitOutTime')) and pd.notna(lap['PitInTime']):
                    duration = (lap['PitOutTime'] - lap['PitInTime']).total_seconds()
                # Try 3: PitOutTime on the next lap for this driver
                if duration is None or pd.isna(duration):
                    drv_laps = all_laps[all_laps['Driver'] == lap['Driver']].sort_values('LapNumber')
                    next_laps = drv_laps[drv_laps['LapNumber'] > lap['LapNumber']]
                    if len(next_laps) > 0:
                        nxt = next_laps.iloc[0]
                        if pd.notna(nxt.get('PitOutTime')):
                            duration = (nxt['PitOutTime'] - lap['PitInTime']).total_seconds()
                durations.append(duration)
            pit_laps['PitDuration'] = durations

            # Drop stops where we couldn't compute duration or value is bogus
            valid_stops = pit_laps[pit_laps['PitDuration'].notna()]
            valid_stops = valid_stops[(valid_stops['PitDuration'] > 0) & (valid_stops['PitDuration'] < 120)]
            if len(valid_stops) == 0:
                return "No pit stop durations available"
            fastest_stops = valid_stops.nsmallest(top_n, 'PitDuration')

            result = f"⚡ Top {top_n} Fastest Pit Stops - {gp} {year}:\n\n"

            for i, (idx, lap) in enumerate(fastest_stops.iterrows(), 1):
                driver = lap['Driver']
                duration = lap['PitDuration']
                lap_num = int(lap['LapNumber'])
                result += f"{i}. {driver} - Lap {lap_num}: {duration:.2f}s\n"

            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 25: FRESH VS USED TIRES
    # ==============================================================================
    
    @mcp.tool()
    def compare_tire_age_performance(year: int, gp: str, driver: str) -> str:
        """Compare lap times on fresh vs used tires."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            laps = session.laps.pick_drivers(driver).pick_accurate()
            
            # Compare early stint (fresh grip) vs late stint (degraded)
            FRESH_THRESHOLD = 5
            fresh_laps = laps[laps['TyreLife'] <= FRESH_THRESHOLD]
            used_laps = laps[laps['TyreLife'] > FRESH_THRESHOLD]

            result = f"🛞 Tire Age Performance - {driver} ({gp} {year}):\n\n"

            if len(fresh_laps) > 0:
                fresh_avg = fresh_laps['LapTime'].mean()
                result += f"Early Stint (laps 1-{FRESH_THRESHOLD} on each set):\n"
                result += f"  Laps: {len(fresh_laps)}\n"
                result += f"  Avg Time: {str(fresh_avg).split('days')[-1].strip()}\n\n"

            if len(used_laps) > 0:
                used_avg = used_laps['LapTime'].mean()
                result += f"Late Stint (lap {FRESH_THRESHOLD + 1}+ on each set):\n"
                result += f"  Laps: {len(used_laps)}\n"
                result += f"  Avg Time: {str(used_avg).split('days')[-1].strip()}\n\n"

            if len(fresh_laps) > 0 and len(used_laps) > 0:
                delta = (used_avg - fresh_avg).total_seconds()
                if delta > 0:
                    result += f"Degradation: +{delta:.3f}s (late stint slower)\n"
                else:
                    result += f"Degradation: {delta:.3f}s (late stint faster)\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 26: PENALTIES & INVESTIGATIONS
    # ==============================================================================
    
    @mcp.tool()
    def get_penalties(year: int, gp: str) -> str:
        """Get all penalties issued during the race."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load(messages=True)
            
            messages = session.race_control_messages
            
            # Filter for penalty-related messages, excluding blue flags
            is_penalty = messages['Message'].str.contains('PENALTY|INVESTIGATION|UNDER INVESTIGATION|TIME PENALTY|DRIVE THROUGH|STOP.GO|BLACK AND WHITE|DISQUALIFIED', case=False, na=False)
            is_blue = messages['Message'].str.contains('BLUE FLAG', case=False, na=False)
            if 'Flag' in messages.columns:
                is_blue = is_blue | messages['Flag'].str.contains('BLUE', case=False, na=False)
            penalties = messages[is_penalty & ~is_blue]
            
            if len(penalties) == 0:
                return "No penalties issued"
            
            result = f"⚖️ Penalties - {gp} {year}:\n\n"
            
            for idx, msg in penalties.iterrows():
                time = msg['Time']
                message = msg['Message']
                result += f"[{time}] {message}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 27: HISTORICAL WINNERS (ERGAST API)
    # ==============================================================================
    
    @mcp.tool()
    def get_race_winners_history(gp: str, years: int = 5) -> str:
        """Get race winners for a specific GP over the last N years."""
        try:
            current_year = datetime.now().year
            start_year = current_year - years + 1
            
            result = f"🏆 Race Winners History - {gp} (Last {years} years):\n\n"
            
            # Resolve circuit ID once using mapping, with API fallback
            mapped_circuit_id = _resolve_circuit_id(gp)
            
            for year in range(current_year, start_year - 1, -1):
                try:
                    circuit_id = mapped_circuit_id
                    
                    if not circuit_id:
                        url = f"https://api.jolpi.ca/ergast/f1/{year}/circuits.json"
                        response = requests.get(url, timeout=15)
                        circuits = response.json()['MRData']['CircuitTable']['Circuits']
                        
                        for circuit in circuits:
                            if gp.lower() in circuit['circuitName'].lower() or gp.lower() in circuit['Location']['locality'].lower():
                                circuit_id = circuit['circuitId']
                                break
                    
                    if not circuit_id:
                        continue
                    
                    # Get results
                    url = f"https://api.jolpi.ca/ergast/f1/{year}/circuits/{circuit_id}/results/1.json"
                    response = requests.get(url, timeout=15)
                    data = response.json()
                    
                    if data['MRData']['RaceTable']['Races']:
                        race = data['MRData']['RaceTable']['Races'][0]
                        winner = race['Results'][0]
                        driver_name = f"{winner['Driver']['givenName']} {winner['Driver']['familyName']}"
                        team = winner['Constructor']['name']
                        result += f"{year}: {driver_name} ({team})\n"
                except Exception:
                    continue
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 28: OVERTAKES DETECTION
    # ==============================================================================
    
    @mcp.tool()
    def detect_overtakes(year: int, gp: str, driver: str) -> str:
        """Detect when a driver overtook others (position gained between laps)."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            laps = session.laps.pick_drivers(driver)
            
            # Get actual grid position from session results
            driver_result = session.results[session.results['Abbreviation'] == driver]
            if not driver_result.empty and pd.notna(driver_result.iloc[0]['GridPosition']):
                grid_pos = int(driver_result.iloc[0]['GridPosition'])
            else:
                grid_pos = int(laps.iloc[0]['Position']) if len(laps) > 0 else None
            
            result = f"🏁 Overtakes - {driver} ({gp} {year}):\n\n"
            if grid_pos is not None:
                result += f"Grid Position: P{grid_pos}\n"
            
            overtakes = []
            # Check Lap 1 position vs grid position
            if len(laps) > 0 and grid_pos is not None:
                lap1_pos = laps.iloc[0]['Position']
                if pd.notna(lap1_pos) and lap1_pos < grid_pos:
                    overtakes.append((1, grid_pos, lap1_pos, int(grid_pos - lap1_pos)))
            
            for i in range(1, len(laps)):
                prev_pos = laps.iloc[i-1]['Position']
                curr_pos = laps.iloc[i]['Position']
                
                if pd.notna(prev_pos) and pd.notna(curr_pos):
                    if curr_pos < prev_pos:  # Position improved (lower number = better)
                        lap_num = int(laps.iloc[i]['LapNumber'])
                        positions_gained = int(prev_pos - curr_pos)
                        overtakes.append((lap_num, prev_pos, curr_pos, positions_gained))
            
            if len(overtakes) == 0:
                return f"{driver} did not gain positions during the race"
            
            total_gained = sum(o[3] for o in overtakes)
            result += f"Total Positions Gained: {total_gained}\n\n"
            
            for lap, old_pos, new_pos, gained in overtakes:
                result += f"Lap {lap}: P{int(old_pos)} → P{int(new_pos)} (+{gained})\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 29: GAP ANALYSIS
    # ==============================================================================
    
    @mcp.tool()
    def get_gap_to_leader(year: int, gp: str, driver: str) -> str:
        """Track gap to race leader throughout the race."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            driver_laps = session.laps.pick_drivers(driver)
            all_laps = session.laps
            
            result = f"⏱️ Gap to Leader - {driver} ({gp} {year}):\n\n"
            
            for idx, lap in driver_laps.iterrows():
                lap_num = int(lap['LapNumber'])
                
                # Find leader at same lap
                same_lap = all_laps[all_laps['LapNumber'] == lap_num]
                leader_lap = same_lap[same_lap['Position'] == 1.0]
                
                if len(leader_lap) > 0 and pd.notna(lap['Time']) and pd.notna(leader_lap.iloc[0]['Time']):
                    gap = (lap['Time'] - leader_lap.iloc[0]['Time']).total_seconds()
                    result += f"Lap {lap_num}: +{gap:.3f}s\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 30: LONG RUN PACE (PRACTICE SESSIONS)
    # ==============================================================================
    
    @mcp.tool()
    def analyze_long_run_pace(year: int, gp: str, driver: str, session: str = 'FP2') -> str:
        """Analyze race simulation pace from practice sessions."""
        try:
            # Sprint weekends only have FP1 — fall back from FP2
            if session == 'FP2':
                event = fastf1.get_event(year, gp)
                if event['EventFormat'] != 'conventional':
                    session = 'FP1'

            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)

            laps = s.laps.pick_drivers(driver)

            # Filter for consecutive laps (race sim)
            long_runs = []
            current_run = []
            
            for idx, lap in laps.iterrows():
                if pd.notna(lap['LapTime']) and not lap.get('PitInTime'):
                    current_run.append(lap)
                else:
                    if len(current_run) >= 5:  # At least 5 consecutive laps
                        long_runs.append(current_run)
                    current_run = []
            
            if len(current_run) >= 5:
                long_runs.append(current_run)
            
            if len(long_runs) == 0:
                return "No long runs found (need 5+ consecutive laps)"
            
            result = f"🏃 Long Run Pace - {driver} ({gp} {year} {session}):\n\n"
            
            for i, run in enumerate(long_runs, 1):
                lap_times = [lap['LapTime'] for lap in run]
                avg_time = pd.Series(lap_times).mean()
                compound = run[0]['Compound']
                
                result += f"Run {i} ({compound}):\n"
                result += f"  Laps: {len(run)}\n"
                result += f"  Avg Time: {str(avg_time).split('days')[-1].strip()}\n\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 31: HEAD-TO-HEAD COMPARISON
    # ==============================================================================
    
    @mcp.tool()
    def team_head_to_head(year: int, gp: str, team: str, session: str = 'Q') -> str:
        """Compare both drivers in a team head-to-head."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            team_laps = s.laps.pick_teams(team)
            
            # Fuzzy match: if exact team name fails, try substring match
            if team_laps.empty:
                all_teams = pd.unique(s.laps['Team'])
                matched = [t for t in all_teams if team.lower() in t.lower()]
                if matched:
                    team_laps = s.laps.pick_teams(matched[0])
                    team = matched[0]
            
            drivers = pd.unique(team_laps['Driver'])
            
            if len(drivers) != 2:
                return "Could not find exactly 2 drivers for this team"
            
            d1, d2 = drivers[0], drivers[1]
            
            d1_fastest = team_laps.pick_drivers(d1).pick_fastest()
            d2_fastest = team_laps.pick_drivers(d2).pick_fastest()

            if d1_fastest is None or d2_fastest is None:
                missing = d1 if d1_fastest is None else d2
                return f"⚔️ Head-to-Head - {team} ({gp} {year} {session}):\n\n{missing} has no valid laps in this session. Try session='R' instead."

            result = f"⚔️ Head-to-Head - {team} ({gp} {year} {session}):\n\n"

            # Lap times
            d1_time = d1_fastest['LapTime'].total_seconds()
            d2_time = d2_fastest['LapTime'].total_seconds()
            delta = abs(d1_time - d2_time)
            faster = d1 if d1_time < d2_time else d2
            
            result += f"{d1}: {d1_time:.3f}s\n"
            result += f"{d2}: {d2_time:.3f}s\n"
            result += f"\nFaster: {faster} by {delta:.3f}s\n\n"
            
            # Sector comparison
            result += "Sector Comparison:\n"
            for i in [1, 2, 3]:
                sector = f'Sector{i}Time'
                s1 = d1_fastest[sector].total_seconds()
                s2 = d2_fastest[sector].total_seconds()
                faster_s = d1 if s1 < s2 else d2
                result += f"S{i}: {faster_s} by {abs(s1-s2):.3f}s\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 32: TRACK RECORDS
    # ==============================================================================
    
    @mcp.tool()
    def get_track_record(gp: str) -> str:
        """Get the all-time lap record for a specific circuit (via Ergast)."""
        try:
            # Use last completed season as default (current season may be in progress)
            year = datetime.now().year - 1
            
            # Try direct mapping first, then fall back to API search
            circuit_id = _resolve_circuit_id(gp)
            circuit_name = gp
            
            if not circuit_id:
                url = f"https://api.jolpi.ca/ergast/f1/{year}/circuits.json"
                response = requests.get(url, timeout=15)
                circuits = response.json()['MRData']['CircuitTable']['Circuits']
                
                for circuit in circuits:
                    if gp.lower() in circuit['circuitName'].lower() or gp.lower() in circuit['Location']['locality'].lower():
                        circuit_id = circuit['circuitId']
                        circuit_name = circuit['circuitName']
                        break
            
            if not circuit_id:
                return f"Circuit '{gp}' not found"
            
            # Get fastest lap
            url = f"https://api.jolpi.ca/ergast/f1/{year}/circuits/{circuit_id}/fastest/1/results.json"
            response = requests.get(url, timeout=15)
            data = response.json()
            
            if data['MRData']['RaceTable']['Races']:
                race = data['MRData']['RaceTable']['Races'][0]
                fastest = race['Results'][0]
                driver = f"{fastest['Driver']['givenName']} {fastest['Driver']['familyName']}"
                time = fastest['FastestLap']['Time']['time']
                
                result = f"🏁 Track Record - {circuit_name}:\n\n"
                result += f"Driver: {driver}\n"
                result += f"Time: {time}\n"
                result += f"Year: {year}\n"
                
                return result
            
            return "Track record data not available"
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 33: SESSION SUMMARY
    # ==============================================================================
    
    @mcp.tool()
    def get_session_summary(year: int, gp: str, session: str = 'R') -> str:
        """Get a comprehensive quick summary of a session."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load()
            
            result = f"📋 Session Summary - {gp} {year} {session}:\n\n"
            
            # Basic info
            result += f"Date: {s.date}\n"
            result += f"Circuit: {s.event.Location}, {s.event.Country}\n\n"
            
            # Winner or pole
            if session in ['R', 'S']:
                winner = s.results.iloc[0]
                result += f"Winner: {winner['Abbreviation']} ({winner['TeamName']})\n"
                if pd.notna(winner['Time']):
                    result += f"Time: {winner['Time']}\n"
            elif session in ['Q', 'SQ', 'SS']:
                pole = s.results.iloc[0]
                result += f"Pole: {pole['Abbreviation']} ({pole['TeamName']})\n"
                if 'Q3' in pole and pd.notna(pole['Q3']):
                    result += f"Time: {pole['Q3']}\n"
            
            result += f"\nTotal Laps: {int(s.laps['LapNumber'].max())}\n"
            result += f"Drivers: {len(s.drivers)}\n"
            
            # Weather summary
            if hasattr(s, 'weather_data') and s.weather_data is not None:
                w = s.weather_data
                result += f"\nWeather:\n"
                result += f"  Air Temp: {w['AirTemp'].mean():.1f}°C\n"
                result += f"  Track Temp: {w['TrackTemp'].mean():.1f}°C\n"
                result += f"  Rainfall: {w['Rainfall'].any()}\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 34: TIRE STRATEGY COMPARISON
    # ==============================================================================
    
    @mcp.tool()
    def compare_strategies(year: int, gp: str, driver1: str, driver2: str) -> str:
        """Compare tire strategies between two drivers."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            d1_laps = session.laps.pick_drivers(driver1)
            d2_laps = session.laps.pick_drivers(driver2)
            
            result = f"📊 Strategy Comparison - {driver1} vs {driver2} ({gp} {year}):\n\n"
            
            # Driver 1 stints
            result += f"{driver1}:\n"
            for stint in d1_laps.groupby('Stint'):
                stint_num = stint[0]
                stint_laps = stint[1]
                compound = stint_laps.iloc[0]['Compound']
                start = int(stint_laps.iloc[0]['LapNumber'])
                end = int(stint_laps.iloc[-1]['LapNumber'])
                result += f"  Stint {int(stint_num)}: {compound} (Laps {start}-{end})\n"
            
            result += f"\n{driver2}:\n"
            for stint in d2_laps.groupby('Stint'):
                stint_num = stint[0]
                stint_laps = stint[1]
                compound = stint_laps.iloc[0]['Compound']
                start = int(stint_laps.iloc[0]['LapNumber'])
                end = int(stint_laps.iloc[-1]['LapNumber'])
                result += f"  Stint {int(stint_num)}: {compound} (Laps {start}-{end})\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 35: STARTING TIRE ANALYSIS
    # ==============================================================================
    
    @mcp.tool()
    def analyze_starting_tires(year: int, gp: str) -> str:
        """Analyze which tire compounds were used at race start."""
        try:
            session = fastf1.get_session(year, gp, 'R')
            session.load()
            
            # Get first lap for each driver
            first_laps = session.laps[session.laps['LapNumber'] == 1]
            
            result = f"🏁 Starting Tire Choices - {gp} {year}:\n\n"
            
            # Group by compound
            for compound in pd.unique(first_laps['Compound']):
                if pd.notna(compound):
                    drivers = first_laps[first_laps['Compound'] == compound]['Driver'].tolist()
                    result += f"{compound}: {', '.join(drivers)}\n"
            
            # Count by compound
            result += "\nSummary:\n"
            compound_counts = first_laps['Compound'].value_counts()
            for compound, count in compound_counts.items():
                if pd.notna(compound):
                    result += f"  {compound}: {count} drivers\n"
            
            return result
        except Exception as e:
            return f"Error: {str(e)}"
    
    # ==============================================================================
    # MODULE 36: BEST PERSONAL LAPS
    # ==============================================================================
    
    @mcp.tool()
    def get_personal_best_laps(year: int, gp: str, session: str = 'Q') -> str:
        """Get each driver's personal best lap time."""
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            
            # Get personal best laps (IsPersonalBest flag)
            pb_laps = s.laps[s.laps['IsPersonalBest'] == True]
            pb_laps = pb_laps.loc[pb_laps.groupby('Driver')['LapTime'].idxmin()].sort_values('LapTime')
            
            result = f"⭐ Personal Best Laps - {gp} {year} {session}:\n\n"
            
            for idx, lap in pb_laps.iterrows():
                driver = lap['Driver']
                team = lap['Team']
                time = str(lap['LapTime']).split('days')[-1].strip()
                result += f"{driver} ({team}): {time}\n"
            
            return result
        except Exception as e:
            return f"Error: {e}"
    
    # ==============================================================================
    # MODULE 28: LIVE TIMING (Real-time Race Data)
    # ==============================================================================
    
    @mcp.tool()
    def get_live_session_status() -> str:
        """Get current live F1 session status and timing information."""
        try:
            # Connect to live timing
            livedata = fastf1.livetiming.data.LiveTimingData()
            
            result = "🔴 LIVE F1 Session Status:\n\n"
            
            # Get session info
            if hasattr(livedata, 'session_info'):
                info = livedata.session_info
                result += f"Session: {info.get('Name', 'Unknown')}\n"
                result += f"Track: {info.get('Meeting', {}).get('Circuit', 'Unknown')}\n"
                result += f"Status: {info.get('Status', 'Unknown')}\n\n"
            
            return result
        except Exception as e:
            return f"No live session active or error: {e}"
    
    @mcp.tool()
    def get_live_positions() -> str:
        """Get current live race positions and gaps."""
        try:
            livedata = fastf1.livetiming.data.LiveTimingData()
            
            result = "🏁 LIVE Race Positions:\n\n"
            
            # Get position data
            if hasattr(livedata, 'position_data'):
                positions = livedata.position_data
                
                for pos, driver_data in sorted(positions.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
                    driver = driver_data.get('Driver', 'Unknown')
                    gap = driver_data.get('GapToLeader', '0.0')
                    interval = driver_data.get('IntervalToPositionAhead', '')
                    
                    result += f"P{pos}: {driver}"
                    if gap != '0.0':
                        result += f" +{gap}s"
                    if interval:
                        result += f" (Δ {interval})"
                    result += "\n"
            
            return result
        except Exception as e:
            return f"No live session active or error: {e}"
    
    @mcp.tool()
    def get_live_lap_times() -> str:
        """Get latest lap times from live session."""
        try:
            livedata = fastf1.livetiming.data.LiveTimingData()
            
            result = "⏱️ LIVE Lap Times:\n\n"
            
            # Get timing data
            if hasattr(livedata, 'timing_data'):
                timing = livedata.timing_data
                
                for driver_num, driver_data in timing.items():
                    driver = driver_data.get('Driver', f'#{driver_num}')
                    last_lap = driver_data.get('LastLapTime', {})
                    
                    if last_lap:
                        lap_time = last_lap.get('Value', 'N/A')
                        personal_best = driver_data.get('BestLapTime', {}).get('Value', 'N/A')
                        
                        result += f"{driver}: {lap_time}"
                        if personal_best != 'N/A':
                            result += f" (PB: {personal_best})"
                        result += "\n"
            
            return result
        except Exception as e:
            return f"No live session active or error: {e}"
    
    @mcp.tool()
    def get_live_sector_times(driver: str) -> str:
        """Get live sector times for a specific driver."""
        try:
            livedata = fastf1.livetiming.data.LiveTimingData()
            
            result = f"🟣 LIVE Sector Times - {driver}:\n\n"
            
            # Find driver in timing data
            if hasattr(livedata, 'timing_data'):
                timing = livedata.timing_data
                
                driver_data = None
                for num, data in timing.items():
                    if data.get('Driver', '').upper() == driver.upper():
                        driver_data = data
                        break
                
                if driver_data:
                    sectors = driver_data.get('Sectors', [])
                    for i, sector in enumerate(sectors, 1):
                        sector_time = sector.get('Value', 'N/A')
                        personal_best = sector.get('PersonalFastest', False)
                        overall_best = sector.get('OverallFastest', False)
                        
                        result += f"Sector {i}: {sector_time}"
                        if personal_best:
                            result += " 🟢 (PB)"
                        if overall_best:
                            result += " 🟣 (Fastest)"
                        result += "\n"
                else:
                    result += f"Driver {driver} not found in live timing"
            
            return result
        except Exception as e:
            return f"No live session active or error: {e}"
    
    @mcp.tool()
    def get_live_telemetry(driver: str) -> str:
        """Get live telemetry data for a specific driver (speed, throttle, etc)."""
        try:
            livedata = fastf1.livetiming.data.LiveTimingData()
            
            result = f"📊 LIVE Telemetry - {driver}:\n\n"
            
            # Get car data from live timing
            if hasattr(livedata, 'car_data'):
                car_data = livedata.car_data
                
                driver_data = None
                for num, data in car_data.items():
                    if data.get('Driver', '').upper() == driver.upper():
                        driver_data = data
                        break
                
                if driver_data:
                    result += f"Speed: {driver_data.get('Speed', 'N/A')} km/h\n"
                    result += f"Gear: {driver_data.get('Gear', 'N/A')}\n"
                    result += f"RPM: {driver_data.get('RPM', 'N/A')}\n"
                    result += f"Throttle: {driver_data.get('Throttle', 'N/A')}%\n"
                    result += f"Brake: {driver_data.get('Brake', 'N/A')}\n"
                    result += f"DRS: {driver_data.get('DRS', 'N/A')}\n"
                else:
                    result += f"Driver {driver} not found in live telemetry"
            
            return result
        except Exception as e:
            return f"No live session active or error: {e}"
    
    @mcp.tool()
    def get_live_weather() -> str:
        """Get current live weather conditions at the track."""
        try:
            livedata = fastf1.livetiming.data.LiveTimingData()
            
            result = "🌤️ LIVE Weather Conditions:\n\n"
            
            # Get weather data
            if hasattr(livedata, 'weather_data'):
                weather = livedata.weather_data
                
                result += f"Air Temp: {weather.get('AirTemp', 'N/A')}°C\n"
                result += f"Track Temp: {weather.get('TrackTemp', 'N/A')}°C\n"
                result += f"Humidity: {weather.get('Humidity', 'N/A')}%\n"
                result += f"Pressure: {weather.get('Pressure', 'N/A')} mbar\n"
                result += f"Wind Speed: {weather.get('WindSpeed', 'N/A')} m/s\n"
                result += f"Wind Direction: {weather.get('WindDirection', 'N/A')}°\n"
                result += f"Rainfall: {weather.get('Rainfall', 'N/A')}\n"
            
            return result
        except Exception as e:
            return f"No live session active or error: {e}"

# =============================================================================
# ENTRY POINT
# =============================================================================


def main():
    """Entry point for the Pitwall MCP server (used by console_scripts and __main__)."""
    import argparse
    parser = argparse.ArgumentParser(description="Pitwall — F1 MCP Server")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    mode = "full" if FASTF1_AVAILABLE else "lite"
    if not FASTF1_AVAILABLE:
        print("Pitwall (lite) — 14 tools loaded. For 67 tools with plots and deep analysis:")
        print('  pip install "f1pitwall[full]"')
        print()
    if args.http:
        print(f"Pitwall ({mode}) starting on {args.host}:{args.port}")
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        print(f"Pitwall ({mode}) starting (stdio)")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
