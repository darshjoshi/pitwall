# Pitwall

**The F1 data command center for Claude.** 22 tools covering race results, lap-by-lap telemetry, tyre strategy, pit stops, weather, race control, driver comparisons, and historical data back to 1950.

Works with **Claude Desktop**, **Claude Code**, and any MCP-compatible client.

![Pitwall Demo — Race Standings and Tyre Strategy](assets/demo-standings.png)

## Quick Start

### Claude Code (2 commands)

```bash
pip install "mcp[cli]" requests
claude mcp add pitwall -- python3 /path/to/pitwall.py
```

Restart Claude Code. Ask anything:
> "Who won the 2026 Chinese GP?"
> "Compare Antonelli vs Russell's pace at China"

### Claude Desktop (copy-paste config)

1. Install dependencies:
```bash
pip install "mcp[cli]" requests
```

2. Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):
```json
{
  "mcpServers": {
    "pitwall": {
      "command": "python3",
      "args": ["/absolute/path/to/pitwall.py"]
    }
  }
}
```

3. Restart Claude Desktop. The tools appear in the tools panel.

## What You Can Ask

| Question | Tool Used |
|----------|-----------|
| "Who won the Chinese GP?" | `get_standings` |
| "What was Verstappen's speed on lap 25?" | `get_telemetry` |
| "Compare Hamilton vs Leclerc at China 2026" | `get_driver_comparison` |
| "What tyres did everyone use?" | `get_tyre_strategy` |
| "Fastest pit stop at Australia 2025?" | `get_pit_stops` |
| "When was the safety car?" | `get_race_control` |
| "Was it raining during the race?" | `get_weather` |
| "Top speeds at Monza 2024?" | `get_speed_traps` |
| "Norris's lap times in the race" | `get_lap_times` |
| "Who won the 2005 championship?" | `get_championship_standings` |

![Pitwall Demo — Lap Telemetry](assets/demo-telemetry.png)

## Tools

### Lite Mode (15 tools — no heavy dependencies)

These use F1's free static archive and Jolpica API. Install: `pip install "mcp[cli]" requests`

| Tool | Description |
|------|-------------|
| `list_seasons` | Available seasons (2018-present) |
| `list_races` | Full season calendar with dates |
| `get_race_info` | Session details and available data feeds |
| `get_standings` | Race classification — positions, gaps, best laps, pits |
| `get_lap_times` | Lap-by-lap times, filterable by driver and lap range |
| `get_telemetry` | Speed, RPM, throttle, brake, gear, DRS for a specific lap |
| `get_tyre_strategy` | Compound, stint length, new/used for every driver |
| `get_pit_stops` | All pit stops sorted by fastest |
| `get_race_control` | Flags, penalties, safety cars, investigations |
| `get_weather` | Air/track temp, rain, humidity, wind |
| `get_speed_traps` | Speed at 4 measurement points per driver |
| `get_driver_comparison` | Head-to-head: position, pace, strategy, pit stops |
| `get_team_radio` | Team radio clip URLs |
| `get_historical_results` | Race results from 1950 to present |
| `get_championship_standings` | Driver/constructor championships from 1950+ |

### Full Mode (22 tools — adds FastF1 analysis + visual plots)

Install: `pip install -r requirements-full.txt`

Everything in Lite, plus:

| Tool | Description |
|------|-------------|
| `get_race_results` | Detailed results via FastF1 |
| `plot_telemetry_comparison` | Speed/throttle/brake traces — returns image |
| `plot_gear_shifts` | Gear shift map on track layout — returns image |
| `get_fastest_lap_data` | Sector times, compound, tyre life |
| `get_driver_standings_fastf1` | Championship standings with points |
| `analyze_lap_consistency` | Mean, std dev, spread analysis |
| `get_session_summary` | Complete session overview |

## Data Sources

All data is **free and requires no API keys or authentication**.

| Source | Data | Coverage |
|--------|------|----------|
| [F1 Static Live Timing](https://livetiming.formula1.com/static/) | Telemetry, timing, strategy, pit stops, weather, race control | 2018-present |
| [Jolpica-F1](https://api.jolpi.ca/ergast/f1/) | Historical results and championships | 1950-present |
| [FastF1](https://github.com/theOehrly/Fast-F1) (optional) | Enhanced telemetry analysis and plots | 2018-present |

## How It Works

Pitwall reads from F1's publicly available static timing archive — the same data that powers the official F1 app. After each session ends (~30 minutes), F1 publishes 33 data feeds per session including full car telemetry (speed, RPM, throttle, brake, gear, DRS at ~4Hz per car), GPS positions, tyre data, pit stops, and race control messages.

The telemetry tool (`get_telemetry`) correlates the timing stream with the car data stream to extract telemetry for a specific driver on a specific lap — something no other MCP server does.

### Architecture

```
Claude ──MCP──> Pitwall ──HTTP──> livetiming.formula1.com/static/ (free)
                       ──HTTP──> api.jolpi.ca/ergast/f1/ (free)
                       ──lib──>  FastF1 (optional, local)
```

### Auto-Degradation

FastF1 not installed? Pitwall still starts with 15 tools — no crashes, no missing dependency errors. Install FastF1 later for visual plots and deep analysis.

```
$ pip install "mcp[cli]" requests    # 15 tools (lite)
$ pip install -r requirements-full.txt  # 22 tools (full)
```

## Race Names

Race names are fuzzy-matched. All of these work:

```
"china", "chinese", "shanghai"          → Chinese Grand Prix
"australia", "melbourne", "aus"         → Australian Grand Prix
"monaco", "monte carlo"                 → Monaco Grand Prix
"silverstone", "uk", "great britain"    → British Grand Prix
```

## Driver Codes

Standard 3-letter codes:

```
VER = Verstappen    HAM = Hamilton    NOR = Norris     LEC = Leclerc
ANT = Antonelli     RUS = Russell     PIA = Piastri    BEA = Bearman
GAS = Gasly         LAW = Lawson      HAD = Hadjar     SAI = Sainz
ALO = Alonso        STR = Stroll      OCO = Ocon       BOT = Bottas
ALB = Albon         HUL = Hulkenberg  COL = Colapinto  LIN = Lindblad
```

## Development

```bash
git clone https://github.com/darshjoshi/pitwall.git
cd pitwall
pip install -r requirements-full.txt

# Run locally
python3 pitwall.py              # stdio mode
python3 pitwall.py --http       # HTTP mode (port 8000)
python3 pitwall.py --http --port 3000  # custom port
```

## Credits

- [FastF1](https://github.com/theOehrly/Fast-F1) by @theOehrly — the gold standard F1 Python library
- [Jolpica-F1](https://github.com/jolpica/jolpica-f1) — the Ergast API successor
- [drivenrajat/f1](https://github.com/drivenrajat/f1) — inspiration for FastF1 tool patterns

## License

MIT
