"""Parse Garmin .FIT files to extract GPS tracks."""

import fitdecode
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_fit_file(filepath: str | Path) -> dict:
    """
    Parse a .FIT file and return GPS trackpoints.

    Returns:
        {
            "points": [(lat, lon), ...],
            "start_time": str (ISO 8601) | None,
            "end_time": str (ISO 8601) | None,
            "filename": str,
            "error": str | None
        }
    """
    filepath = Path(filepath)
    result = {
        "points": [],
        "start_time": None,
        "end_time": None,
        "filename": filepath.name,
        "error": None,
    }

    try:
        points = []
        first_time = None
        last_time = None

        with fitdecode.FitReader(str(filepath)) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue

                # Extract GPS coordinates and timestamp from record messages
                if frame.name == "record":
                    lat = None
                    lon = None
                    ts = None
                    for field in frame.fields:
                        if field.name == "position_lat" and field.value is not None:
                            # FIT stores lat/lon in semicircles, convert to degrees
                            lat = field.value * (180.0 / 2**31)
                        elif field.name == "position_long" and field.value is not None:
                            lon = field.value * (180.0 / 2**31)
                        elif field.name == "timestamp" and field.value is not None:
                            ts = field.value

                    if lat is not None and lon is not None:
                        # Sanity check: valid GPS coordinates
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            points.append((lat, lon))
                            if ts is not None:
                                if first_time is None:
                                    first_time = ts
                                last_time = ts

        result["points"] = points
        if first_time is not None:
            if isinstance(first_time, datetime):
                result["start_time"] = first_time.isoformat()
            else:
                result["start_time"] = str(first_time)
        if last_time is not None:
            if isinstance(last_time, datetime):
                result["end_time"] = last_time.isoformat()
            else:
                result["end_time"] = str(last_time)

        logger.info(
            f"Parsed {filepath.name}: {len(points)} points"
        )

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Failed to parse {filepath.name}: {e}")

    return result
