"""Watch import directory for new activity files and process them."""

import asyncio
import shutil
import os
import logging
from pathlib import Path

from app.database import (
    file_hash, file_already_imported, insert_file, mark_tiles_dirty
)
from app.parser import parse_activity_file, SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

IMPORT_DIR = Path("/data/import")
DONE_DIR = Path("/data/import/done")
ERROR_DIR = Path("/data/import/errors")
POLL_INTERVAL = int(os.environ.get("IMPORT_POLL_SECONDS", "30"))
MIN_ZOOM = int(os.environ.get("TILE_MIN_ZOOM", "2"))
MAX_ZOOM = int(os.environ.get("TILE_MAX_ZOOM", "18"))


def _move_file(filepath: Path, dest_dir: Path):
    """Move a file into a destination directory, handling name collisions."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filepath.name
    # Handle name collisions by appending a counter
    if dest.exists():
        stem = filepath.stem
        suffix = filepath.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    shutil.move(str(filepath), str(dest))
    logger.debug(f"Moved {filepath.name} → {dest}")


def _import_one_activity(filepath: Path, fhash: str, parsed: dict,
                         track_index: int | None = None) -> dict:
    """Import a single parsed activity into the database.

    track_index is set when the source file contained multiple tracks,
    so the hash is made unique per track.
    """
    # Make hash unique per track within a multi-track file
    effective_hash = (f"{fhash}:track{track_index}"
                      if track_index is not None else fhash)

    result = {
        "filename": parsed["filename"],
        "status": "unknown",
        "num_points": 0,
        "error": None,
    }

    if file_already_imported(effective_hash):
        result["status"] = "duplicate"
        logger.info(f"Skipping duplicate: {parsed['filename']}")
        return result

    if parsed.get("error"):
        result["status"] = "error"
        result["error"] = parsed["error"]
        return result

    if not parsed["points"]:
        result["status"] = "no_gps_data"
        return result

    # Store in database
    insert_file(
        filename=parsed["filename"],
        fhash=effective_hash,
        points=parsed["points"]
    )

    # Mark affected tiles as dirty using ALL points — no sampling.
    mark_tiles_dirty(
        parsed["points"],
        min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM
    )

    result["status"] = "imported"
    result["num_points"] = len(parsed["points"])

    logger.info(
        f"Imported {parsed['filename']}: {len(parsed['points'])} points"
    )
    return result


def import_single_file(filepath: Path, move_after: bool = True) -> dict | list[dict]:
    """Import a single activity file (.fit, .gpx, .tcx).

    Returns a single result dict, or a list of result dicts if the file
    contained multiple tracks (e.g. a multi-track GPX).

    If move_after is True, the file is moved to done/ (on success or
    duplicate) or errors/ (on failure) after processing.
    """
    try:
        fhash = file_hash(str(filepath))

        # Parse the activity file — may return a list for multi-track GPX
        parsed = parse_activity_file(filepath)

        # Normalize to a list of activities
        if isinstance(parsed, list):
            activities = parsed
        else:
            activities = [parsed]

        results = []
        if len(activities) == 1:
            # Single activity — use file hash directly
            results.append(_import_one_activity(filepath, fhash, activities[0]))
        else:
            # Multi-track file — each track gets a unique hash
            logger.info(
                f"Multi-track file {filepath.name}: {len(activities)} tracks"
            )
            for i, activity in enumerate(activities):
                results.append(
                    _import_one_activity(filepath, fhash, activity,
                                         track_index=i)
                )

        if move_after:
            any_error = any(r["status"] == "error" for r in results)
            _move_file(filepath, ERROR_DIR if any_error else DONE_DIR)

        return results[0] if len(results) == 1 else results

    except Exception as e:
        logger.error(f"Failed to import {filepath.name}: {e}")
        if move_after:
            try:
                _move_file(filepath, ERROR_DIR)
            except Exception:
                pass
        return {
            "filename": filepath.name,
            "status": "error",
            "num_points": 0,
            "error": str(e),
        }

def _flatten_results(result) -> list[dict]:
    """Normalize import_single_file output to a flat list of dicts."""
    if isinstance(result, list):
        return result
    return [result]


def scan_import_directory() -> list[dict]:
    """Scan the import directory for new activity files and import them.

    Skips the done/ and errors/ subdirectories.
    """
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
    skip_dirs = {DONE_DIR, ERROR_DIR}
    results = []

    for path in sorted(IMPORT_DIR.rglob("*")):
        # Skip files inside done/ or errors/
        if any(path.is_relative_to(sd) for sd in skip_dirs):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS and path.is_file():
            result = import_single_file(path)
            results.extend(_flatten_results(result))

    imported_count = sum(1 for r in results if r["status"] == "imported")
    if imported_count > 0:
        logger.info(f"Import scan complete: {imported_count} new files imported")

    return results


async def watch_import_directory():
    """Background task that periodically scans for new FIT files."""
    logger.info(f"Starting import watcher (poll every {POLL_INTERVAL}s)")
    while True:
        try:
            # Run the synchronous scan in a thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, scan_import_directory)
        except Exception as e:
            logger.error(f"Import watcher error: {e}")
        await asyncio.sleep(POLL_INTERVAL)
