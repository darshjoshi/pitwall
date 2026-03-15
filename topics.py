"""
F1 Live Timing data topics and their metadata.

Topics suffixed with .z contain base64-encoded, raw-deflate-compressed data.
Auth-gated topics require an F1 TV subscription token (since Aug 2025).
"""

# Channel mapping for CarData.z telemetry
CAR_DATA_CHANNELS = {
    0: "rpm",
    2: "speed",        # km/h
    3: "gear",         # 0=neutral, 1-8
    4: "throttle",     # 0-100%
    5: "brake",        # 0-100
    45: "drs",         # DRS status
}

# All known topics with metadata
TOPICS = {
    # --- Free topics (no auth needed) ---
    "Heartbeat":            {"compressed": False, "auth_required": False},
    "SessionInfo":          {"compressed": False, "auth_required": False},
    "SessionStatus":        {"compressed": False, "auth_required": False},
    "SessionData":          {"compressed": False, "auth_required": False},
    "TrackStatus":          {"compressed": False, "auth_required": False},
    "DriverList":           {"compressed": False, "auth_required": False},
    "TimingData":           {"compressed": False, "auth_required": False},
    "TimingAppData":        {"compressed": False, "auth_required": False},
    "TimingStats":          {"compressed": False, "auth_required": False},
    "WeatherData":          {"compressed": False, "auth_required": False},
    "RaceControlMessages":  {"compressed": False, "auth_required": False},
    "ExtrapolatedClock":    {"compressed": False, "auth_required": False},
    "TopThree":             {"compressed": False, "auth_required": False},
    "LapCount":             {"compressed": False, "auth_required": False},
    "TeamRadio":            {"compressed": False, "auth_required": False},
    "AudioStreams":         {"compressed": False, "auth_required": False},
    "ContentStreams":       {"compressed": False, "auth_required": False},
    "RcmSeries":           {"compressed": False, "auth_required": False},

    # --- Auth-gated topics (F1 TV subscription required since Aug 2025) ---
    "CarData.z":   {"compressed": True, "auth_required": True},
    "Position.z":  {"compressed": True, "auth_required": True},
}

# Topics available without authentication
FREE_TOPICS = [name for name, meta in TOPICS.items() if not meta["auth_required"]]

# All topics (including auth-gated)
ALL_TOPICS = list(TOPICS.keys())

# Additional feeds available only in the static API (post-session archives)
STATIC_ONLY_FEEDS = [
    "ArchiveStatus",
    "ChampionshipPrediction",
    "TimingDataF1",
    "DriverTracker",
    "LapSeries",
    "TyreStintSeries",
    "DriverRaceInfo",
    "WeatherDataSeries",
    "TlaRcm",
    "CurrentTyres",
    "OvertakeSeries",
    "PitLaneTimeCollection",
    "PitStop",
    "PitStopSeries",
]
