"""Parse Garmin .FIT files to extract GPS tracks."""

import fitdecode
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_fit_file(filepath: str | Path) -> dict:
    """
    Parse a .FIT file and return GPS trackpoints.

    Returns:
        {
            "points": [(lat, lon), ...],
            "filename": str,
            "error": str | None
        }
    """
    filepath = Path(filepath)
    result = {
        "points": [],
        "filename": filepath.name,
        "error": None,
    }

    try:
        points = []

        with fitdecode.FitReader(str(filepath)) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue

                # Extract GPS coordinates from record messages
                if frame.name == "record":
                    lat = None
                    lon = None
                    for field in frame.fields:
                        if field.name == "position_lat" and field.value is not None:
                            # FIT stores lat/lon in semicircles, convert to degrees
                            lat = field.value * (180.0 / 2**31)
                        elif field.name == "position_long" and field.value is not None:
                            lon = field.value * (180.0 / 2**31)

                    if lat is not None and lon is not None:
                        # Sanity check: valid GPS coordinates
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            points.append((lat, lon))

        result["points"] = points

        logger.info(
            f"Parsed {filepath.name}: {len(points)} points"
        )

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Failed to parse {filepath.name}: {e}")

    return result
