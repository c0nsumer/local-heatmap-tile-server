"""Unified parser dispatcher for activity files (FIT, GPX, TCX)."""

from pathlib import Path

from app.fit_parser import parse_fit_file
from app.gpx_parser import parse_gpx_file
from app.tcx_parser import parse_tcx_file

SUPPORTED_EXTENSIONS = {".fit", ".gpx", ".tcx"}

_PARSERS = {
    ".fit": parse_fit_file,
    ".gpx": parse_gpx_file,
    ".tcx": parse_tcx_file,
}


def parse_activity_file(filepath: str | Path) -> dict:
    """Parse an activity file, dispatching to the correct parser by extension.

    Returns the standard result dict:
        {
            "points": [(lat, lon), ...],
            "filename": str,
            "error": str | None
        }
    """
    filepath = Path(filepath)
    ext = filepath.suffix.lower()

    parser = _PARSERS.get(ext)
    if parser is None:
        return {
            "points": [],
            "filename": filepath.name,
            "error": f"Unsupported format: {ext}. Accepted: .fit, .gpx, .tcx",
        }

    return parser(filepath)
