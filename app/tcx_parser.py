"""Parse TCX files to extract GPS tracks."""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

# TCX namespace used by Garmin and most devices
_TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"


def _find_activities(root: ET.Element) -> list[ET.Element]:
    """Find Activity elements, trying with and without namespace."""
    # Try with namespace first
    activities = root.findall(f".//{{{_TCX_NS}}}Activity")
    if activities:
        return activities

    # Fallback: try without namespace (some non-Garmin exporters omit it)
    activities = root.findall(".//Activity")
    return activities


def _find_trackpoints(activity: ET.Element) -> list[ET.Element]:
    """Find Trackpoint elements within an Activity."""
    # Try with namespace
    trackpoints = activity.findall(
        f".//{{{_TCX_NS}}}Lap/{{{_TCX_NS}}}Track/{{{_TCX_NS}}}Trackpoint"
    )
    if trackpoints:
        return trackpoints

    # Fallback without namespace
    trackpoints = activity.findall(".//Lap/Track/Trackpoint")
    return trackpoints


def _get_position(trackpoint: ET.Element) -> tuple[float, float] | None:
    """Extract lat/lon from a Trackpoint element."""
    # Try with namespace
    pos = trackpoint.find(f"{{{_TCX_NS}}}Position")
    if pos is None:
        pos = trackpoint.find("Position")
    if pos is None:
        return None

    lat_el = pos.find(f"{{{_TCX_NS}}}LatitudeDegrees")
    if lat_el is None:
        lat_el = pos.find("LatitudeDegrees")

    lon_el = pos.find(f"{{{_TCX_NS}}}LongitudeDegrees")
    if lon_el is None:
        lon_el = pos.find("LongitudeDegrees")

    if lat_el is None or lon_el is None:
        return None
    if lat_el.text is None or lon_el.text is None:
        return None

    try:
        lat = float(lat_el.text)
        lon = float(lon_el.text)
    except ValueError:
        return None

    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def parse_tcx_file(filepath: str | Path) -> dict:
    """
    Parse a .TCX file and return GPS trackpoints.

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
        tree = ET.parse(filepath)
        root = tree.getroot()

        activities = _find_activities(root)
        if not activities:
            result["error"] = "No Activity elements found in TCX file"
            return result

        # Extract GPS points from all activities
        points = []
        for activity in activities:
            for tp in _find_trackpoints(activity):
                pos = _get_position(tp)
                if pos is not None:
                    points.append(pos)

        result["points"] = points

        logger.info(
            f"Parsed {filepath.name}: {len(points)} points"
        )

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Failed to parse {filepath.name}: {e}")

    return result
