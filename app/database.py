"""SQLite database for GPS trackpoints and import metadata."""

import sqlite3
import hashlib
import os
import threading
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("/data/db/heatmap.db")

# Thread-local storage for connection reuse
_local = threading.local()


def get_db_path() -> Path:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def _get_thread_conn() -> sqlite3.Connection:
    """Get or create a SQLite connection for the current thread."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(get_db_path()), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA cache_size=-4000")  # 4MB page cache
        conn.execute("PRAGMA mmap_size=33554432")  # 32MB memory-mapped I/O
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


@contextmanager
def get_conn():
    conn = _get_thread_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                file_hash TEXT UNIQUE NOT NULL,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                num_points INTEGER DEFAULT 0,
                start_time TEXT,
                end_time TEXT,
                bounds_min_lat REAL,
                bounds_min_lon REAL,
                bounds_max_lat REAL,
                bounds_max_lon REAL
            );

            CREATE TABLE IF NOT EXISTS trackpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_trackpoints_file
                ON trackpoints(file_id);

            CREATE INDEX IF NOT EXISTS idx_files_hash
                ON files(file_hash);

            CREATE TABLE IF NOT EXISTS tile_dirty (
                z INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                PRIMARY KEY (z, x, y)
            );
        """)

        # R-tree spatial index for fast bounding-box lookups.
        # Each entry maps a trackpoint id to its lat/lon as a
        # zero-area rectangle (min=max).  The R-tree is purpose-built
        # for "find all points in this rectangle" queries, replacing
        # the B-tree index on (lat, lon).
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS trackpoints_rtree
            USING rtree(
                id,              -- matches trackpoints.id
                min_lat, max_lat,
                min_lon, max_lon
            )
        """)

        # Drop old B-tree spatial indexes — R-tree replaces them
        conn.execute("DROP INDEX IF EXISTS idx_trackpoints_latlon")
        conn.execute("DROP INDEX IF EXISTS idx_trackpoints_lat_lon_fileid")


def file_hash(filepath: str) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def file_already_imported(fhash: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM files WHERE file_hash = ?", (fhash,)
        ).fetchone()
        return row is not None


def insert_file(filename: str, fhash: str,
                points: list[tuple[float, float]],
                start_time: str | None = None,
                end_time: str | None = None) -> int:
    """Insert a file record and its trackpoints. Returns file_id."""
    if not points:
        return -1

    # Compute bounds in a single pass without creating separate lists
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    for lat, lon in points:
        if lat < min_lat: min_lat = lat
        if lat > max_lat: max_lat = lat
        if lon < min_lon: min_lon = lon
        if lon > max_lon: max_lon = lon

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO files
               (filename, file_hash, num_points, start_time, end_time,
                bounds_min_lat, bounds_min_lon, bounds_max_lat, bounds_max_lon)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (filename, fhash, len(points), start_time, end_time,
             min_lat, min_lon, max_lat, max_lon)
        )
        file_id = cur.lastrowid

        # Insert trackpoints using a generator (no intermediate list)
        conn.executemany(
            "INSERT INTO trackpoints (file_id, lat, lon) VALUES (?, ?, ?)",
            ((file_id, lat, lon) for lat, lon in points)
        )

        # Get the range of IDs just inserted so we can populate the R-tree
        last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        first_id = last_id - len(points) + 1

        # Bulk-insert into R-tree using a generator (no intermediate list)
        conn.executemany(
            """INSERT INTO trackpoints_rtree
               (id, min_lat, max_lat, min_lon, max_lon)
               VALUES (?, ?, ?, ?, ?)""",
            ((first_id + i, lat, lat, lon, lon)
             for i, (lat, lon) in enumerate(points))
        )

    return file_id


MAX_POINTS_PER_TILE = int(os.environ.get("MAX_POINTS_PER_TILE", "200000"))


def get_track_segments_in_tile(z: int, x: int, y: int,
                                padding_factor: float = 0.1
                                ) -> list[list[tuple[float, float]]]:
    """Get track segments (ordered points per file) within a tile bbox.

    Uses an R-tree spatial index for fast bounding-box lookup, then
    fetches a window of points per file including the immediate neighbors
    outside the tile so that line segments crossing tile boundaries are
    drawn without gaps.
    """
    import math

    n = 2 ** z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))

    dlat = (lat_max - lat_min) * padding_factor
    dlon = (lon_max - lon_min) * padding_factor
    lat_min -= dlat
    lat_max += dlat
    lon_min -= dlon
    lon_max += dlon

    with get_conn() as conn:
        # Step 1: Use R-tree to find trackpoint IDs in the bounding box,
        # then join with trackpoints to get file_id and compute min/max
        # ID ranges per file.
        hit_rows = conn.execute(
            """SELECT t.file_id, MIN(r.id) as min_id, MAX(r.id) as max_id
               FROM trackpoints_rtree r
               JOIN trackpoints t ON t.id = r.id
               WHERE r.min_lat >= ? AND r.max_lat <= ?
                 AND r.min_lon >= ? AND r.max_lon <= ?
               GROUP BY t.file_id
               LIMIT ?""",
            (lat_min, lat_max, lon_min, lon_max,
             MAX_POINTS_PER_TILE)
        ).fetchall()

        if not hit_rows:
            return []

        # Step 2: For each file, fetch points from (min_id - 1) to
        # (max_id + 1) so we include the neighbor points just outside the
        # tile.  This ensures line segments crossing the boundary are
        # continuous.
        segments = []
        for hr in hit_rows:
            fid = hr["file_id"]
            lo = hr["min_id"] - 1  # one point before the tile area
            hi = hr["max_id"] + 1  # one point after the tile area
            rows = conn.execute(
                """SELECT lat, lon FROM trackpoints
                   WHERE file_id = ? AND id BETWEEN ? AND ?
                   ORDER BY id""",
                (fid, lo, hi)
            ).fetchall()
            if len(rows) >= 2:
                segments.append([(r["lat"], r["lon"]) for r in rows])

    return segments


def get_stats() -> dict:
    with get_conn() as conn:
        total_tracks = conn.execute("SELECT COUNT(*) as c FROM files").fetchone()["c"]
        total_points = conn.execute("SELECT COUNT(*) as c FROM trackpoints").fetchone()["c"]
        # Count distinct source files: multi-track entries have hashes
        # like "abc:track0", "abc:track1" — strip the suffix to count
        # unique source files.
        total_files = conn.execute(
            """SELECT COUNT(DISTINCT
                   CASE WHEN file_hash LIKE '%:track%'
                        THEN SUBSTR(file_hash, 1, INSTR(file_hash, ':track') - 1)
                        ELSE file_hash
                   END
               ) as c FROM files"""
        ).fetchone()["c"]

        # Date range from activity timestamps
        date_row = conn.execute(
            """SELECT MIN(start_time) as earliest,
                      MAX(end_time) as latest
               FROM files
               WHERE start_time IS NOT NULL"""
        ).fetchone()

    result = {
        "total_files": total_files,
        "total_tracks": total_tracks,
        "total_points": total_points,
    }

    if date_row and date_row["earliest"]:
        result["date_range"] = {
            "earliest": date_row["earliest"],
            "latest": date_row["latest"],
        }

    return result


def mark_tiles_dirty(points: list[tuple[float, float]],
                     min_zoom: int = 2, max_zoom: int = 16):
    """Mark tiles that need re-rendering after new data import."""
    import math

    tiles_to_mark = set()
    for lat, lon in points:
        for z in range(min_zoom, max_zoom + 1):
            n = 2 ** z
            x = int((lon + 180.0) / 360.0 * n)
            lat_rad = math.radians(lat)
            y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
            x = max(0, min(n - 1, x))
            y = max(0, min(n - 1, y))
            tiles_to_mark.add((z, x, y))

    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO tile_dirty (z, x, y) VALUES (?, ?, ?)",
            list(tiles_to_mark)
        )


def get_dirty_tiles(limit: int = 500) -> list[tuple[int, int, int]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT z, x, y FROM tile_dirty LIMIT ?", (limit,)
        ).fetchall()
    return [(r["z"], r["x"], r["y"]) for r in rows]


def clear_dirty_tile(z: int, x: int, y: int):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM tile_dirty WHERE z=? AND x=? AND y=?",
            (z, x, y)
        )


def count_dirty_tiles() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) as c FROM tile_dirty").fetchone()["c"]


def mark_all_tiles_dirty(min_zoom: int = None, max_zoom: int = None):
    """Mark every tile that contains data as dirty, for a full re-render.

    Uses SQL-level sampling (every Nth row) to avoid loading all trackpoints
    into Python memory at once.  The sample target scales with max_zoom so
    that high-zoom tiles are not missed.
    """
    import math

    if min_zoom is None:
        min_zoom = int(os.environ.get("TILE_MIN_ZOOM", "2"))
    if max_zoom is None:
        max_zoom = int(os.environ.get("TILE_MAX_ZOOM", "18"))

    # Scale sample target with zoom — higher zoom needs denser sampling
    # to avoid missing small tiles.  At z18 a tile is ~600m wide, so we
    # need a point roughly every 300m to guarantee coverage.
    SAMPLE_TARGET = 5000 * (2 ** max(0, max_zoom - 14))

    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trackpoints"
        ).fetchone()["cnt"]

        if count == 0:
            return 0

        step = max(1, count // SAMPLE_TARGET)

        tiles_to_mark = set()

        # Sample every Nth row in SQL to avoid loading all rows into memory
        cursor = conn.execute(
            """SELECT lat, lon FROM (
                   SELECT lat, lon, ROW_NUMBER() OVER (ORDER BY id) as rn
                   FROM trackpoints
               ) WHERE rn % ? = 0""",
            (step,)
        )

        for row in cursor:
            lat, lon = row["lat"], row["lon"]
            for z in range(min_zoom, max_zoom + 1):
                n = 2 ** z
                x = int((lon + 180.0) / 360.0 * n)
                lat_rad = math.radians(lat)
                y = int((1.0 - math.log(math.tan(lat_rad) +
                        1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
                x = max(0, min(n - 1, x))
                y = max(0, min(n - 1, y))
                tiles_to_mark.add((z, x, y))

        conn.executemany(
            "INSERT OR IGNORE INTO tile_dirty (z, x, y) VALUES (?, ?, ?)",
            list(tiles_to_mark)
        )

    return len(tiles_to_mark)


def get_all_data_tile_coords(min_zoom: int = None, max_zoom: int = None,
                              chunk_size: int = 100000) -> set[tuple[int, int, int]]:
    """Enumerate ALL tile coordinates that contain trackpoint data.

    Unlike mark_all_tiles_dirty() which samples, this iterates through every
    trackpoint in chunks to guarantee no tiles are missed.  Uses NumPy
    vectorization for speed.  Used by the PMTiles export to ensure
    completeness.

    Returns a set of (z, x, y) tuples.
    """
    import numpy as np
    import logging
    logger = logging.getLogger(__name__)

    if min_zoom is None:
        min_zoom = int(os.environ.get("TILE_MIN_ZOOM", "2"))
    if max_zoom is None:
        max_zoom = int(os.environ.get("TILE_MAX_ZOOM", "18"))

    tiles = set()

    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM trackpoints").fetchone()["c"]
        if total == 0:
            return tiles

        logger.info(f"Scanning {total} trackpoints for tile coordinates (z{min_zoom}-z{max_zoom})...")
        offset = 0
        while offset < total:
            rows = conn.execute(
                "SELECT lat, lon FROM trackpoints ORDER BY id LIMIT ? OFFSET ?",
                (chunk_size, offset)
            ).fetchall()

            if not rows:
                break

            # Vectorize with numpy for speed
            lats = np.array([r["lat"] for r in rows], dtype=np.float64)
            lons = np.array([r["lon"] for r in rows], dtype=np.float64)
            lat_rad = np.radians(lats)
            # Pre-compute the mercator Y term (shared across zoom levels)
            merc_y = (1.0 - np.log(np.tan(lat_rad) +
                      1.0 / np.cos(lat_rad)) / np.pi) / 2.0

            for z in range(min_zoom, max_zoom + 1):
                n = 2 ** z
                xs = np.clip(((lons + 180.0) / 360.0 * n).astype(np.int32),
                             0, n - 1)
                ys = np.clip((merc_y * n).astype(np.int32), 0, n - 1)

                # Use unique pairs to reduce set insertions
                coords = np.column_stack((xs, ys))
                unique = np.unique(coords, axis=0)
                for row in unique:
                    tiles.add((z, int(row[0]), int(row[1])))

            offset += chunk_size
            if offset % 500000 == 0:
                logger.info(f"  ...processed {offset}/{total} trackpoints, {len(tiles)} tile coords so far")

        logger.info(f"Found {len(tiles)} total tile coordinates")

    return tiles
