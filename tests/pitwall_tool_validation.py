#!/usr/bin/env python3
"""
Integration validation for Pitwall MCP tools (non-live only).

Runs every tool except get_live_*, cross-checks static vs FastF1 classification,
and sanity-checks image outputs.

Usage:
  cd /path/to/pitwall && python3 tests/pitwall_tool_validation.py
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

# Stable matplotlib cache inside repo
os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.path.dirname(__file__), "..", ".mplconfig"))

# Project root on path
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pitwall  # noqa: E402

try:
    from mcp.types import ImageContent
except ImportError:
    ImageContent = None  # type: ignore

# --- Golden fixtures ---
# US GP 2024: static archive includes PitStopSeries (Italy 2024 does not — pit data is PitLane-only).
# FastF1 key for COTA is 'Austin'.
YEAR = 2024
RACE_STATIC = "austin"
GP = "Austin"
D1, D2 = "VER", "LEC"
TEAM = "Ferrari"

YEAR_SPRINT = 2024
GP_SPRINT = "Shanghai"


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str = ""
    ms: float = 0.0


@dataclass
class RunReport:
    results: list[CaseResult] = field(default_factory=list)
    cross_checks: list[tuple[str, bool, str]] = field(default_factory=list)


def _extract_standings_tlas(text: str) -> list[str]:
    """Parse TLA order from get_standings() text (P1 first)."""
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^P\s*(\d+)\s+([A-Z]{3})\s", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    out.sort(key=lambda x: x[0])
    return [t for _, t in out]


def _extract_race_results_tlas(text: str) -> list[str]:
    """Parse finishing order from get_race_results() pandas string."""
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] in ("ClassifiedPosition", "===") or not parts[0][0].isdigit():
            continue
        if not parts[0].isdigit():
            continue
        pos = int(parts[0])
        tla = parts[1]
        if len(tla) == 3 and tla.isupper():
            out.append((pos, tla))
    out.sort(key=lambda x: x[0])
    return [t for _, t in out]


def _looks_like_error(s: str) -> bool:
    s = s.strip()
    return s.startswith("Error:") or s.startswith("Error ") or "Traceback" in s


def _is_good_image(obj: Any) -> bool:
    if ImageContent is None:
        return hasattr(obj, "mimeType") and getattr(obj, "mimeType", "") == "image/png" and bool(
            getattr(obj, "data", None)
        )
    return isinstance(obj, ImageContent) and obj.mimeType == "image/png" and bool(obj.data)


def run_cross_checks(report: RunReport) -> None:
    """Compare independent sources for the same event."""
    try:
        st = pitwall.get_standings(year=YEAR, race=RACE_STATIC, session_type="Race")
        rr = pitwall.get_race_results(YEAR, GP)
        a = _extract_standings_tlas(st)
        b = _extract_race_results_tlas(rr)
        if len(a) < 3 or len(b) < 3:
            report.cross_checks.append(
                (
                    "podium_standings_vs_race_results",
                    False,
                    f"parse short: standings={len(a)} race_results={len(b)}",
                )
            )
        else:
            match = a[:10] == b[:10]
            report.cross_checks.append(
                (
                    "top10_standings_vs_race_results",
                    match,
                    f"standings[:10]={a[:10]} vs results[:10]={b[:10]}",
                )
            )
    except Exception as e:
        report.cross_checks.append(("podium_standings_vs_race_results", False, str(e)))

    # Driver comparison: both drivers should appear in standings
    try:
        st = pitwall.get_standings(year=YEAR, race=RACE_STATIC, session_type="Race")
        body = st.upper()
        ok = D1 in body and D2 in body
        report.cross_checks.append(("standings_contains_D1_D2", ok, f"{D1}/{D2} in standings text"))
    except Exception as e:
        report.cross_checks.append(("standings_contains_D1_D2", False, str(e)))


def _call(name: str, fn: Callable[[], Any]) -> CaseResult:
    import time

    t0 = time.perf_counter()
    try:
        out = fn()
        ms = (time.perf_counter() - t0) * 1000
        if isinstance(out, str):
            if _looks_like_error(out) and "No live session" not in out:
                return CaseResult(name, False, out[:500], ms)
            if len(out.strip()) < 2 and name not in ("list_seasons",):
                return CaseResult(name, False, "empty or trivial response", ms)
            return CaseResult(name, True, f"len={len(out)}", ms)
        if out is None:
            return CaseResult(name, False, "None", ms)
        if _is_good_image(out):
            return CaseResult(name, True, "ImageContent png", ms)
        return CaseResult(name, True, repr(type(out)), ms)
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return CaseResult(name, False, f"{e}\n{traceback.format_exc()}", ms)


def discover_lap() -> int:
    """Use telemetry lap=0 to list laps; pick a middle race lap."""
    t = pitwall.get_telemetry(D1, year=YEAR, race=RACE_STATIC, lap=0, session_type="Race")
    m = re.search(r"Available:\s*\[([^\]]+)\]", t)
    if not m:
        return 5
    nums = re.findall(r"\d+", m.group(1))
    laps = [int(x) for x in nums if int(x) > 0]
    if not laps:
        return 5
    return laps[len(laps) // 3]


def build_tool_calls(lap: int) -> list[tuple[str, Callable[[], Any]]]:
    """All non-live tools with sensible arguments."""
    calls: list[tuple[str, Callable[[], Any]]] = [
        ("list_seasons", lambda: pitwall.list_seasons()),
        ("list_races", lambda: pitwall.list_races(YEAR)),
        ("get_race_info", lambda: pitwall.get_race_info(YEAR, RACE_STATIC, "Race")),
        ("get_standings", lambda: pitwall.get_standings(YEAR, RACE_STATIC, "Race")),
        ("get_lap_times", lambda: pitwall.get_lap_times(YEAR, RACE_STATIC, D1, "Race", 1, 15)),
        ("get_telemetry", lambda: pitwall.get_telemetry(D1, YEAR, RACE_STATIC, lap, "Race")),
        ("get_tyre_strategy", lambda: pitwall.get_tyre_strategy(YEAR, RACE_STATIC, "Race")),
        ("get_pit_stops", lambda: pitwall.get_pit_stops(YEAR, RACE_STATIC, "Race")),
        ("get_race_control", lambda: pitwall.get_race_control(YEAR, RACE_STATIC, "Race", "")),
        ("get_weather", lambda: pitwall.get_weather(YEAR, RACE_STATIC, "Race")),
        ("get_speed_traps", lambda: pitwall.get_speed_traps(YEAR, RACE_STATIC, "Race")),
        ("get_driver_comparison", lambda: pitwall.get_driver_comparison(D1, D2, YEAR, RACE_STATIC, "Race")),
        ("get_historical_results", lambda: pitwall.get_historical_results(YEAR, "", "")),
        ("get_championship_standings_driver", lambda: pitwall.get_championship_standings(YEAR, "driver")),
        ("get_championship_standings_constructor", lambda: pitwall.get_championship_standings(YEAR, "constructor")),
    ]

    if not getattr(pitwall, "FASTF1_AVAILABLE", False):
        return calls

    ff: list[tuple[str, Callable[[], Any]]] = [
        ("get_schedule", lambda: pitwall.get_schedule(YEAR)),
        ("get_session_info", lambda: pitwall.get_session_info(YEAR, GP, "R")),
        ("get_race_results", lambda: pitwall.get_race_results(YEAR, GP)),
        ("get_fastest_lap_data", lambda: pitwall.get_fastest_lap_data(YEAR, GP, D1, "Q")),
        ("plot_telemetry_comparison", lambda: pitwall.plot_telemetry_comparison(YEAR, GP, D1, D2, "Q")),
        ("plot_multi_telemetry_comparison", lambda: pitwall.plot_multi_telemetry_comparison(YEAR, GP, D1, 2, 8, "R")),
        ("plot_driver_telemetry_comparison", lambda: pitwall.plot_driver_telemetry_comparison(YEAR, GP, D1, D2, lap, "R")),
        ("plot_gear_shifts", lambda: pitwall.plot_gear_shifts(YEAR, GP, D1, "Q")),
        ("get_weather_data", lambda: pitwall.get_weather_data(YEAR, GP, "R")),
        ("get_circuit_info", lambda: pitwall.get_circuit_info(YEAR, GP)),
        ("get_driver_tyre_detail", lambda: pitwall.get_driver_tyre_detail(YEAR, GP, D1)),
        ("get_pit_stop_detail_driver", lambda: pitwall.get_pit_stop_detail(YEAR, GP, D1)),
        ("get_pit_stop_detail_all", lambda: pitwall.get_pit_stop_detail(YEAR, GP, None)),
        ("get_driver_standings", lambda: pitwall.get_driver_standings(YEAR, None)),
        ("get_constructor_standings", lambda: pitwall.get_constructor_standings(YEAR, None)),
        ("get_sprint_results", lambda: pitwall.get_sprint_results(YEAR_SPRINT, GP_SPRINT)),
        ("compare_sector_times", lambda: pitwall.compare_sector_times(YEAR, GP, D1, D2, "Q")),
        ("get_lap_times_fastf1", lambda: pitwall.get_lap_times_fastf1(YEAR, GP, D1, "R")),
        ("get_deleted_laps", lambda: pitwall.get_deleted_laps(YEAR, GP, "Q")),
        ("get_position_changes", lambda: pitwall.get_position_changes(YEAR, GP, D1)),
        ("get_track_status", lambda: pitwall.get_track_status(YEAR, GP, "R")),
        ("get_race_control_messages", lambda: pitwall.get_race_control_messages(YEAR, GP, "R")),
        ("get_driver_info", lambda: pitwall.get_driver_info(YEAR, GP, D1)),
        ("get_team_laps", lambda: pitwall.get_team_laps(YEAR, GP, TEAM, "R")),
        ("get_speed_trap_comparison", lambda: pitwall.get_speed_trap_comparison(YEAR, GP, "Q")),
        ("analyze_drs_usage", lambda: pitwall.analyze_drs_usage(YEAR, GP, D1, "R")),
        ("compare_tire_compounds", lambda: pitwall.compare_tire_compounds(YEAR, GP, "R")),
        ("get_stint_analysis", lambda: pitwall.get_stint_analysis(YEAR, GP, D1)),
        ("get_dnf_list", lambda: pitwall.get_dnf_list(YEAR, GP)),
        ("get_fastest_sectors", lambda: pitwall.get_fastest_sectors(YEAR, GP, "Q")),
        ("compare_grid_to_finish", lambda: pitwall.compare_grid_to_finish(YEAR, GP)),
        ("get_qualifying_progression", lambda: pitwall.get_qualifying_progression(YEAR, GP)),
        ("analyze_lap_consistency", lambda: pitwall.analyze_lap_consistency(YEAR, GP, D1, "R")),
        ("analyze_brake_points", lambda: pitwall.analyze_brake_points(YEAR, GP, D1, "Q")),
        ("analyze_rpm_data", lambda: pitwall.analyze_rpm_data(YEAR, GP, D1, "Q")),
        ("get_fastest_pit_stops", lambda: pitwall.get_fastest_pit_stops(YEAR, GP, 10)),
        ("compare_tire_age_performance", lambda: pitwall.compare_tire_age_performance(YEAR, GP, D1)),
        ("get_penalties", lambda: pitwall.get_penalties(YEAR, GP)),
        ("get_race_winners_history", lambda: pitwall.get_race_winners_history(GP, 5)),
        ("detect_overtakes", lambda: pitwall.detect_overtakes(YEAR, GP, D1)),
        ("get_gap_to_leader", lambda: pitwall.get_gap_to_leader(YEAR, GP, D1)),
        ("analyze_long_run_pace", lambda: pitwall.analyze_long_run_pace(YEAR, GP, D1, "FP2")),
        ("team_head_to_head", lambda: pitwall.team_head_to_head(YEAR, GP, TEAM, "Q")),
        ("get_track_record", lambda: pitwall.get_track_record(GP)),
        ("get_session_summary", lambda: pitwall.get_session_summary(YEAR, GP, "R")),
        ("compare_strategies", lambda: pitwall.compare_strategies(YEAR, GP, D1, D2)),
        ("analyze_starting_tires", lambda: pitwall.analyze_starting_tires(YEAR, GP)),
        ("get_personal_best_laps", lambda: pitwall.get_personal_best_laps(YEAR, GP, "Q")),
    ]
    return calls + ff


def main() -> int:
    report = RunReport()

    if not getattr(pitwall, "FASTF1_AVAILABLE", False):
        print("WARNING: FastF1 not installed — only lite tools will run.")

    lap = discover_lap()
    print(f"Using lap={lap} for telemetry/plots (from static feed lap=0 probe)\n")

    for name, fn in build_tool_calls(lap):
        r = _call(name, fn)
        report.results.append(r)
        status = "OK " if r.ok else "FAIL"
        print(f"[{status}] {name:40s} {r.ms:8.0f}ms  {r.detail[:80]}")

    print("\n--- Cross-checks (static vs FastF1) ---\n")
    run_cross_checks(report)
    for label, ok, detail in report.cross_checks:
        print(f"[{'OK ' if ok else 'FAIL'}] {label}: {detail}")

    failed = [r for r in report.results if not r.ok]
    cc_fail = [c for c in report.cross_checks if not c[1]]

    print(f"\n--- Summary: {len(report.results) - len(failed)}/{len(report.results)} tools passed, {len(cc_fail)} cross-check(s) failed ---")

    if failed:
        print("\nFailed tools:")
        for r in failed:
            print(f"  - {r.name}: {r.detail[:200]}")

    return 1 if (failed or cc_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
