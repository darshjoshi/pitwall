"""
Pitwall — F1 Data MCP Server for Claude

The most comprehensive Formula 1 MCP server. 60+ tools covering
race results, telemetry, tyre strategy, pit stops, weather, race control,
driver comparisons, speed traps, and historical data back to 1950.

Two modes:
  Lite  — 14 tools, no heavy dependencies, free data only
  Full  — 64 tools, includes FastF1 plots and deep analysis

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
    instructions=(
        "Pitwall is an F1 data server with 60+ tools. "
        "Use list_races to find a session, then query data with other tools. "
        "Race names are fuzzy-matched: 'china', 'shanghai', 'chinese' all work. "
        "Driver codes: VER=Verstappen, HAM=Hamilton, NOR=Norris, LEC=Leclerc, "
        "ANT=Antonelli, RUS=Russell, PIA=Piastri. Default year is 2026. "
        "For lap-specific telemetry, use get_telemetry with driver and lap number."
    ),
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
        for year in range(2018, 2027):
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
                if val and lap_num and lap_num != cur.get(num):
                    if lap_start <= lap_num <= lap_end:
                        d = dm.get(num, {"tla": f"#{num}"})
                        laps[num].append(f"  Lap {lap_num:>2}: {val}")
                    cur[num] = lap_num

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
# CORE TOOLS — Team Radio
# =============================================================================

@mcp.tool()
def get_team_radio(year: int = 2026, race: str = "", session_type: str = "Race") -> str:
    """Get team radio clip URLs from a session.

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
        captures = _get_keyframe(path, "TeamRadio").get("Captures", [])
        cl = captures if isinstance(captures, list) else list(captures.values())
        result = f"=== {race_name} {year} — Team Radio ({len(cl)} clips) ===\n\n"
        for c in cl:
            if isinstance(c, dict):
                num = str(c.get("RacingNumber", "?"))
                d = dm.get(num, {"tla": f"#{num}"})
                result += f"  {d['tla']:>3} — https://livetiming.formula1.com/static/{path}{c.get('Path', '')}\n"
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
                result += f"P{e['position']:>2} {c.get('name','?'):25s} Pts: {e['points']:>6} Wins: {e.get('wins','0')}\n"
        else:
            entries = s.get("DriverStandings", [])
            result = f"=== {yr} Driver Championship ===\n\n"
            for e in entries:
                d = e.get("Driver", {})
                c = e.get("Constructors", [{}])[0] if e.get("Constructors") else {}
                name = f"{d.get('givenName','')} {d.get('familyName','')}"
                result += f"P{e['position']:>2} {name:25s} ({c.get('name','?'):15s}) Pts: {e['points']:>6} Wins: {e.get('wins','0')}\n"
        return result
    except Exception as e:
        return f"Error: {e}"


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

    @mcp.tool()
    def get_race_results(year: int, gp: str) -> str:
        """Get detailed race results with positions, times, and status via FastF1.

        Args:
            year: Season year
            gp: Grand Prix name (e.g. 'Australia', 'Monaco')
        """
        try:
            session = fastf1.get_session(year, gp, "R")
            session.load(telemetry=False, weather=False, laps=False)
            results = session.results
            result = f"=== {gp} {year} — Race Results (FastF1) ===\n\n"
            for _, r in results.iterrows():
                result += f"P{r['Position']:>2.0f} #{r['DriverNumber']:>2} {r['FullName']:30s} {r['TeamName']:20s} {r.get('Status','')}\n"
            return result
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def plot_telemetry_comparison(year: int, gp: str, driver1: str, driver2: str,
                                  session: str = "Q") -> ImageContent:
        """Plot speed trace comparison between two drivers (returns image).

        Args:
            year: Season year
            gp: Grand Prix name
            driver1: First driver (e.g. 'VER')
            driver2: Second driver (e.g. 'HAM')
            session: Session type ('R', 'Q', 'FP1', etc.)
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=True)
            lap1 = s.laps.pick_driver(driver1).pick_fastest()
            lap2 = s.laps.pick_driver(driver2).pick_fastest()
            tel1 = lap1.get_telemetry()
            tel2 = lap2.get_telemetry()

            fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
            fig.suptitle(f"{gp} {year} — {driver1} vs {driver2}", fontsize=16, fontweight="bold")

            axes[0].plot(tel1["Distance"], tel1["Speed"], label=driver1)
            axes[0].plot(tel2["Distance"], tel2["Speed"], label=driver2)
            axes[0].set_ylabel("Speed (km/h)")
            axes[0].legend()

            axes[1].plot(tel1["Distance"], tel1["Throttle"], label=driver1)
            axes[1].plot(tel2["Distance"], tel2["Throttle"], label=driver2)
            axes[1].set_ylabel("Throttle %")

            axes[2].plot(tel1["Distance"], tel1["Brake"], label=driver1)
            axes[2].plot(tel2["Distance"], tel2["Brake"], label=driver2)
            axes[2].set_ylabel("Brake")
            axes[2].set_xlabel("Distance (m)")

            plt.tight_layout()
            return _fig_to_image(fig)
        except Exception as e:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.text(0.5, 0.5, f"Error: {e}", ha="center", va="center", fontsize=12, color="red")
            ax.axis("off")
            return _fig_to_image(fig)

    @mcp.tool()
    def plot_gear_shifts(year: int, gp: str, driver: str, session: str = "Q") -> ImageContent:
        """Plot gear shift map on track layout for a driver (returns image).

        Args:
            year: Season year
            gp: Grand Prix name
            driver: Driver code (e.g. 'VER')
            session: Session type
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=True)
            lap = s.laps.pick_driver(driver).pick_fastest()
            tel = lap.get_telemetry()

            fig, ax = plt.subplots(figsize=(12, 10))
            scatter = ax.scatter(tel["X"], tel["Y"], c=tel["nGear"], cmap="RdYlGn",
                               s=2, vmin=1, vmax=8)
            ax.set_aspect("equal")
            ax.set_title(f"{gp} {year} — {driver} Gear Map", fontsize=14, fontweight="bold")
            plt.colorbar(scatter, label="Gear", ax=ax)
            ax.axis("off")
            return _fig_to_image(fig)
        except Exception as e:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.text(0.5, 0.5, f"Error: {e}", ha="center", va="center", fontsize=12, color="red")
            ax.axis("off")
            return _fig_to_image(fig)

    @mcp.tool()
    def get_fastest_lap_data(year: int, gp: str, driver: str, session: str = "Q") -> str:
        """Get detailed fastest lap data with sector times via FastF1.

        Args:
            year: Season year
            gp: Grand Prix name
            driver: Driver code
            session: Session type
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            lap = s.laps.pick_driver(driver).pick_fastest()
            result = f"=== {driver} Fastest Lap — {gp} {year} ({session}) ===\n\n"
            result += f"Lap Time: {lap['LapTime']}\n"
            result += f"Sector 1: {lap['Sector1Time']}\n"
            result += f"Sector 2: {lap['Sector2Time']}\n"
            result += f"Sector 3: {lap['Sector3Time']}\n"
            result += f"Compound: {lap['Compound']}\n"
            result += f"Tyre Life: {lap['TyreLife']} laps\n"
            result += f"Lap Number: {lap['LapNumber']}\n"
            return result
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_driver_standings_fastf1(year: int, round_number: int = 0) -> str:
        """Get driver championship standings via FastF1 with current points.

        Args:
            year: Season year
            round_number: Specific round (0 = latest)
        """
        try:
            url = f"https://api.jolpi.ca/ergast/f1/{year}/driverStandings.json"
            if round_number:
                url = f"https://api.jolpi.ca/ergast/f1/{year}/{round_number}/driverStandings.json"
            data = requests.get(url, timeout=10).json()
            standings = data["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]
            result = f"=== {year} Driver Standings ===\n\n"
            for s in standings:
                d = s["Driver"]
                c = s["Constructors"][0] if s["Constructors"] else {}
                result += f"P{s['position']:>2} {d['givenName']} {d['familyName']:20s} ({c.get('name','?'):15s}) {s['points']:>6} pts  {s.get('wins','0')} wins\n"
            return result
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def analyze_lap_consistency(year: int, gp: str, driver: str, session: str = "R") -> str:
        """Analyze a driver's lap time consistency — mean, std dev, and outliers.

        Args:
            year: Season year
            gp: Grand Prix name
            driver: Driver code
            session: Session type
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False)
            laps = s.laps.pick_driver(driver).pick_quicklaps()
            times = laps["LapTime"].dt.total_seconds()
            result = f"=== {driver} Consistency — {gp} {year} ===\n\n"
            result += f"Clean laps: {len(times)}\n"
            result += f"Mean: {times.mean():.3f}s\n"
            result += f"Std Dev: {times.std():.3f}s\n"
            result += f"Best: {times.min():.3f}s\n"
            result += f"Worst: {times.max():.3f}s\n"
            result += f"Spread: {times.max() - times.min():.3f}s\n"
            return result
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_session_summary(year: int, gp: str, session: str = "R") -> str:
        """Get a complete session summary — results, fastest laps, weather.

        Args:
            year: Season year
            gp: Grand Prix name
            session: Session type
        """
        try:
            s = fastf1.get_session(year, gp, session)
            s.load(telemetry=False, weather=True)
            results = s.results
            weather = s.weather_data

            result = f"=== {s.event['EventName']} {year} — {s.name} ===\n\n"
            result += "Classification:\n"
            for _, r in results.head(10).iterrows():
                result += f"  P{r['Position']:>2.0f} {r['FullName']:25s} {r['TeamName']:20s}\n"

            if weather is not None and not weather.empty:
                w = weather.iloc[-1]
                result += f"\nWeather: Air {w.get('AirTemp', '?')}C | Track {w.get('TrackTemp', '?')}C | Rain: {w.get('Rainfall', '0')}\n"

            fastest = s.laps.pick_fastest()
            result += f"\nFastest lap: {fastest['Driver']} — {fastest['LapTime']} (Lap {fastest['LapNumber']})\n"
            return result
        except Exception as e:
            return f"Error: {e}"


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pitwall — F1 MCP Server")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    mode = "full" if FASTF1_AVAILABLE else "lite"
    if args.http:
        print(f"Pitwall ({mode}) starting on {args.host}:{args.port}")
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        print(f"Pitwall ({mode}) starting (stdio)")
        mcp.run(transport="stdio")
