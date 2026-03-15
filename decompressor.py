"""
Decompression for F1 .z topics (CarData.z, Position.z).

F1's compressed topics use: base64 encoding → raw deflate compression.
The key trick is zlib with -MAX_WBITS (raw deflate, no header).
"""

import base64
import json
import zlib

from f1livetiming.topics import CAR_DATA_CHANNELS


def decompress_z_data(encoded: str) -> dict:
    """Decompress a base64-encoded, raw-deflate-compressed F1 data payload.

    Args:
        encoded: Base64-encoded string from a .z topic

    Returns:
        Parsed JSON dict of the decompressed data
    """
    raw_bytes = base64.b64decode(encoded)
    # -zlib.MAX_WBITS = raw deflate (no zlib/gzip header)
    decompressed = zlib.decompress(raw_bytes, -zlib.MAX_WBITS)
    return json.loads(decompressed)


def parse_car_data(decompressed: dict) -> list[dict]:
    """Parse decompressed CarData.z into human-readable telemetry entries.

    Returns a flat list of per-car telemetry snapshots:
    [{"timestamp": ..., "driver_number": "1", "speed": 298, "rpm": 10234, ...}, ...]
    """
    results = []
    for entry in decompressed.get("Entries", []):
        timestamp = entry.get("Utc", "")
        for driver_num, car in entry.get("Cars", {}).items():
            channels = car.get("Channels", {})
            row = {"timestamp": timestamp, "driver_number": driver_num}
            for channel_id, field_name in CAR_DATA_CHANNELS.items():
                row[field_name] = channels.get(str(channel_id), channels.get(channel_id))
            results.append(row)
    return results


def parse_position_data(decompressed: dict) -> list[dict]:
    """Parse decompressed Position.z into per-car position entries.

    Returns:
    [{"timestamp": ..., "driver_number": "1", "x": 1234, "y": 5678, "z": 90, "status": "OnTrack"}, ...]
    """
    results = []
    for frame in decompressed.get("Position", []):
        timestamp = frame.get("Timestamp", "")
        for driver_num, pos in frame.get("Entries", {}).items():
            results.append({
                "timestamp": timestamp,
                "driver_number": driver_num,
                "x": pos.get("X"),
                "y": pos.get("Y"),
                "z": pos.get("Z"),
                "status": pos.get("Status", ""),
            })
    return results
