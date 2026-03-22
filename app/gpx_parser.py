"""Parse GPX files to extract GPS tracks."""

import logging
from pathlib import Path

import gpxpy

logger = logging.getLogger(__name__)


def parse_gpx_file(filepath: str | Path) -> dict | list[dict]:
    """
    Parse a .GPX file and return GPS trackpoints.

    Only extracts <trk> (recorded tracks), not <rte> (planned routes).

    If the file contains multiple tracks, returns a list of result dicts
    (one per track).  If it contains a single track, returns a single dict
    for backward compatibility.

    Returns:
        dict or list[dict], each with:
        {
            "points": [(lat, lon), ...],
            "filename": str,
            "error": str | None
        }
    """
    filepath = Path(filepath)

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            gpx = gpxpy.parse(f)

        if not gpx.tracks:
            return {
                "points": [],
                "filename": filepath.name,
                "error": None,
            }

        results = []
        for i, track in enumerate(gpx.tracks):
            points = []
            for segment in track.segments:
                for point in segment.points:
                    lat = point.latitude
                    lon = point.longitude
                    if lat is not None and lon is not None:
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            points.append((lat, lon))

            # Build a descriptive filename for multi-track files
            if len(gpx.tracks) > 1:
                track_name = track.name or f"track {i + 1}"
                filename = f"{filepath.stem} [{track_name}]{filepath.suffix}"
            else:
                filename = filepath.name

            results.append({
                "points": points,
                "filename": filename,
                "error": None,
            })

            logger.info(
                f"Parsed {filename}: {len(points)} points"
            )

        # Single track: return a plain dict for compatibility
        if len(results) == 1:
            return results[0]

        return results

    except Exception as e:
        logger.error(f"Failed to parse {filepath.name}: {e}")
        return {
            "points": [],
            "filename": filepath.name,
            "error": str(e),
        }
