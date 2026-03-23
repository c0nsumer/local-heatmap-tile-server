"""Watch import directory for new activity files and process them."""

import asyncio
import gc
import shutil
import os
import logging
from pathlib import Path

from app.database import (
    file_hash, file_already_imported, insert_file, mark_tiles_dirty,
    _delete_cached_tiles, compute_content_hash, content_hash_exists,
    increment_counter
)
from app.parser import parse_activity_file, SUPPORTED_EXTENSIONS
from app.gpx_parser import parse_gpx_file_streaming
from app.prerender import set_prerender_enabled

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
                         track_index: int | None = None,
                         delete_cached: bool = True) -> dict:
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

    # Content-based deduplication: hash the GPS points themselves
    content_hash = compute_content_hash(parsed["points"])
    is_dup, existing_name = content_hash_exists(content_hash)
    if is_dup:
        result["status"] = "content_duplicate"
        increment_counter("content_duplicates_blocked")
        logger.info(
            f"Skipping content-duplicate: {parsed['filename']} "
            f"matches existing track {existing_name}"
        )
        return result

    # Store in database
    insert_file(
        filename=parsed["filename"],
        fhash=effective_hash,
        points=parsed["points"],
        start_time=parsed.get("start_time"),
        end_time=parsed.get("end_time"),
        content_hash=content_hash,
    )

    # Mark affected tiles as dirty using ALL points — no sampling.
    dirty = mark_tiles_dirty(
        parsed["points"],
        min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM,
        delete_cached=delete_cached
    )

    result["status"] = "imported"
    result["num_points"] = len(parsed["points"])

    logger.info(
        f"Imported {parsed['filename']}: {len(parsed['points'])} points"
    )
    return result


def _import_gpx_streaming(filepath: Path, fhash: str,
                          move_after: bool = True) -> list[dict]:
    """Import a GPX file using streaming parsing.

    Processes one track at a time to keep memory usage low, even for
    multi-GB GPX exports with thousands of tracks.
    """
    results = []
    track_count = 0
    any_error = False

    try:
        for track in parse_gpx_file_streaming(filepath):
            track_index = track_count
            track_count += 1

            # Build descriptive filename — we always use [track_name]
            # format since we don't know the total count ahead of time.
            # For single-track files this will be cleaned up after.
            track_name = track.get("track_name") or f"track {track_count}"
            track["filename"] = (
                f"{filepath.stem} [{track_name}]{filepath.suffix}"
            )

            logger.info(
                f"Track {track_count}: {track_name} — "
                f"{len(track['points'])} points"
            )

            result = _import_one_activity(
                filepath, fhash, track, track_index=track_index,
                delete_cached=False
            )
            results.append(result)

            if result["status"] == "error":
                any_error = True

            # Explicitly free the track's points to release memory
            # before parsing the next track
            track["points"] = None
            del track
            gc.collect()

    except Exception as e:
        logger.error(f"Failed to parse {filepath.name}: {e}")
        results.append({
            "filename": filepath.name,
            "status": "error",
            "num_points": 0,
            "error": str(e),
        })
        any_error = True

    # If only one track was found, simplify the filename in the result
    if len(results) == 1 and results[0].get("filename"):
        results[0]["filename"] = filepath.name

    if track_count == 0 and not results:
        results.append({
            "filename": filepath.name,
            "status": "no_gps_data",
            "num_points": 0,
            "error": None,
        })

    # Batch-delete stale cached tiles once, rather than per-track
    if any(r["status"] == "imported" for r in results):
        from app.database import get_dirty_tiles, count_dirty_tiles
        dirty_count = count_dirty_tiles()
        if dirty_count > 0:
            dirty_tiles = set(get_dirty_tiles(limit=dirty_count))
            _delete_cached_tiles(dirty_tiles)

    if move_after:
        try:
            _move_file(filepath, ERROR_DIR if any_error else DONE_DIR)
        except Exception:
            pass

    total_points = sum(r.get("num_points", 0) for r in results)
    imported = sum(1 for r in results if r["status"] == "imported")
    logger.info(
        f"GPX streaming import complete: {filepath.name} — "
        f"{track_count} tracks, {imported} imported, "
        f"{total_points} total points"
    )

    return results


def import_single_file(filepath: Path, move_after: bool = True) -> dict | list[dict]:
    """Import a single activity file (.fit, .gpx, .tcx).

    Returns a single result dict, or a list of result dicts if the file
    contained multiple tracks (e.g. a multi-track GPX).

    If move_after is True, the file is moved to done/ (on success or
    duplicate) or errors/ (on failure) after processing.
    """
    try:
        fhash = file_hash(str(filepath))

        # Use streaming parser for GPX files to handle large exports
        if filepath.suffix.lower() == ".gpx":
            results = _import_gpx_streaming(filepath, fhash, move_after)
            return results[0] if len(results) == 1 else results

        # For FIT/TCX files, use the standard parser
        parsed = parse_activity_file(filepath)

        # Normalize to a list of activities
        if isinstance(parsed, list):
            activities = parsed
        else:
            activities = [parsed]

        total_points = sum(len(a["points"]) for a in activities)
        logger.info(
            f"Parsed {filepath.name}: "
            f"{len(activities)} track{'s' if len(activities) != 1 else ''}, "
            f"{total_points} points"
        )

        results = []
        if len(activities) == 1:
            # Single activity — use file hash directly
            results.append(_import_one_activity(filepath, fhash, activities[0]))
        else:
            # Multi-track file — each track gets a unique hash
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

    Pauses the pre-renderer during import to avoid wasted work (tiles
    rendered mid-import may be dirtied again by subsequent tracks).
    The pre-renderer resumes automatically when the import finishes.

    Skips the done/ and errors/ subdirectories.
    """
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
    skip_dirs = {DONE_DIR, ERROR_DIR}

    # Collect files to import first
    files_to_import = sorted(
        p for p in IMPORT_DIR.rglob("*")
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
        and p.is_file()
        and not any(p.is_relative_to(sd) for sd in skip_dirs)
    )

    if not files_to_import:
        return []

    # Pause pre-renderer during import
    set_prerender_enabled(False)
    logger.info("Paused pre-renderer for import")

    results = []
    try:
        for path in files_to_import:
            result = import_single_file(path)
            results.extend(_flatten_results(result))
    finally:
        # Always resume pre-renderer, even if import fails
        set_prerender_enabled(True)
        logger.info("Resumed pre-renderer after import")

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
