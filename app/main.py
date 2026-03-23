"""GPS Heatmap Tile Server — FastAPI application."""

VERSION = 1

import asyncio
import io
import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response, HTMLResponse, JSONResponse, FileResponse
from PIL import Image

from app.database import (
    init_db, get_track_segments_in_tile, get_stats,
    mark_all_tiles_dirty, count_dirty_tiles,
    get_counter
)
from app.renderer import (
    render_tile, load_cached_tile, save_tile, clear_tile_cache, TILE_SIZE,
    VALID_STYLES
)
from app.importer import (
    scan_import_directory, import_single_file
)
from app.parser import SUPPORTED_EXTENSIONS
from app.prerender import (
    prerender_worker, get_prerender_status, set_prerender_enabled
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Transparent 256x256 PNG (for empty tiles)
EMPTY_TILE = None

VIEWER_HTML = Path(__file__).parent / "viewer.html"
STATUS_HTML = Path(__file__).parent / "status.html"

# Dedicated thread pool for serving tile requests — kept separate from
# the default pool so prerender work can't starve live HTTP requests.
_io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tile-io")


def generate_empty_tile() -> bytes:
    """Generate a transparent PNG tile."""
    img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    global EMPTY_TILE
    logger.info("Initializing database...")
    init_db()
    EMPTY_TILE = generate_empty_tile()

    # Start only the prerender worker — importing is now triggered via API.
    prerender_task = asyncio.create_task(prerender_worker())

    yield

    prerender_task.cancel()
    try:
        await prerender_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="GPS Heatmap Tile Server",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def viewer():
    """Serve the Leaflet map viewer."""
    return VIEWER_HTML.read_text()


@app.get("/manager", response_class=HTMLResponse)
async def manager_page():
    """Serve the data manager dashboard."""
    return STATUS_HTML.read_text()


@app.get("/export/{filename}")
async def download_export(filename: str):
    """Download an exported PMTiles file."""
    export_path = Path("/data/export") / filename
    if not export_path.exists() or not export_path.name.endswith(".pmtiles"):
        raise HTTPException(404, "Export file not found")
    return FileResponse(
        export_path,
        media_type="application/octet-stream",
        filename=filename,
    )


@app.get("/tiles/{style}/{z}/{x}/{y}.png")
async def get_tile(style: str, z: int, x: int, y: int):
    """
    Serve a heatmap tile.

    Checks cache first, renders on-the-fly if not cached.
    """
    if style not in VALID_STYLES:
        raise HTTPException(404, f"Unknown style: {style}")

    if z < 0 or z > 20:
        raise HTTPException(400, "Zoom level out of range")

    n = 2 ** z
    if x < 0 or x >= n or y < 0 or y >= n:
        return Response(content=EMPTY_TILE, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=300"})

    # Check cache (run in dedicated I/O pool, separate from prerender)
    loop = asyncio.get_event_loop()
    cached = await loop.run_in_executor(_io_pool, load_cached_tile, style, z, x, y)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300",
                     "X-Tile-Source": "cache"}
        )

    # Render on-the-fly (also in I/O pool so it doesn't compete with prerender)
    logger.info(f"On-the-fly render: {style}/{z}/{x}/{y}")
    segments = await loop.run_in_executor(
        _io_pool, get_track_segments_in_tile, z, x, y
    )

    if not segments:
        return Response(
            content=EMPTY_TILE,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"}
        )

    img = await loop.run_in_executor(
        _io_pool, render_tile, segments, z, x, y, style
    )

    if img is None:
        return Response(
            content=EMPTY_TILE,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"}
        )

    # Save to cache (encodes PNG and populates memory cache)
    await loop.run_in_executor(_io_pool, save_tile, img, style, z, x, y)
    logger.info(f"On-the-fly render complete: {style}/{z}/{x}/{y} ({len(segments)} segments)")

    # Retrieve bytes from memory cache (just populated by save_tile)
    # instead of re-encoding the image to PNG a second time
    tile_bytes = await loop.run_in_executor(
        _io_pool, load_cached_tile, style, z, x, y
    )

    return Response(
        content=tile_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300",
                 "X-Tile-Source": "rendered"}
    )


@app.post("/api/import")
async def upload_activity_files(files: list[UploadFile] = File(...)):
    """Upload and import activity files (FIT, GPX, TCX) via API."""
    from app.prerender import set_prerender_enabled
    results = []
    import_dir = Path("/data/import")
    import_dir.mkdir(parents=True, exist_ok=True)

    # Pause pre-renderer during upload import
    set_prerender_enabled(False)
    try:
        for upload in files:
            if Path(upload.filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                results.append({
                    "filename": upload.filename,
                    "status": "skipped",
                    "error": "Unsupported format. Accepted: .fit, .gpx, .tcx"
                })
                continue

            # Save uploaded file
            dest = import_dir / upload.filename
            content = await upload.read()
            dest.write_bytes(content)

            # Import it (may return a list for multi-track GPX files)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, import_single_file, dest)
            if isinstance(result, list):
                results.extend(result)
            else:
                results.append(result)
    finally:
        set_prerender_enabled(True)

    return JSONResponse({"results": results})


@app.post("/api/scan")
async def scan_and_import():
    """Scan the import directory for new .FIT files and import them."""
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, scan_import_directory)
    imported = [r for r in results if r["status"] == "imported"]
    content_dupes = [r for r in results if r["status"] == "content_duplicate"]
    dupes = [r for r in results if r["status"] == "duplicate"]
    return JSONResponse({
        "status": "ok",
        "total_scanned": len(results),
        "imported": len(imported),
        "duplicates": len(dupes),
        "content_duplicates": len(content_dupes),
        "results": results,
    })


@app.get("/api/stats")
async def api_stats():
    """Get import statistics."""
    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(None, get_stats)

    # Also compute global bounds
    from app.database import get_conn
    def _get_bounds():
        with get_conn() as conn:
            row = conn.execute(
                """SELECT MIN(bounds_min_lat) as min_lat,
                          MIN(bounds_min_lon) as min_lon,
                          MAX(bounds_max_lat) as max_lat,
                          MAX(bounds_max_lon) as max_lon
                   FROM files"""
            ).fetchone()
            if row and row["min_lat"] is not None:
                return {
                    "min_lat": row["min_lat"],
                    "min_lon": row["min_lon"],
                    "max_lat": row["max_lat"],
                    "max_lon": row["max_lon"],
                }
            return None

    bounds = await loop.run_in_executor(None, _get_bounds)
    stats["bounds"] = bounds
    stats["version"] = VERSION
    stats["duplicates_blocked"] = await loop.run_in_executor(
        None, get_counter, "content_duplicates_blocked"
    )
    return JSONResponse(stats)


@app.post("/api/rebuild")
async def rebuild_all_tiles():
    """Clear all tile caches and queue everything for pre-rendering."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, clear_tile_cache, None)
    marked = await loop.run_in_executor(None, mark_all_tiles_dirty)
    return JSONResponse({
        "status": "ok",
        "message": f"Cache cleared, {marked} tiles queued for pre-rendering"
    })


@app.post("/api/rebuild/{style}")
async def rebuild_style_tiles(style: str):
    """Clear tile cache for a specific style and queue for pre-rendering."""
    if style not in VALID_STYLES:
        raise HTTPException(404, f"Unknown style: {style}")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, clear_tile_cache, style)
    marked = await loop.run_in_executor(None, mark_all_tiles_dirty)
    return JSONResponse({
        "status": "ok",
        "message": f"Cache cleared for {style}, {marked} tiles queued"
    })


@app.get("/api/prerender/status")
async def prerender_status():
    """Get the current state of the pre-render worker."""
    loop = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, get_prerender_status)
    return JSONResponse(status)


@app.post("/api/prerender/pause")
async def prerender_pause():
    """Pause the pre-render worker."""
    set_prerender_enabled(False)
    return JSONResponse({"status": "ok", "state": "paused"})


@app.post("/api/prerender/resume")
async def prerender_resume():
    """Resume the pre-render worker."""
    set_prerender_enabled(True)
    return JSONResponse({"status": "ok", "state": "resumed"})




@app.post("/api/export/pmtiles")
async def export_pmtiles(style: str = "warm"):
    """Export rendered tiles as a PMTiles archive for a given style.

    Enumerates all tile coordinates that contain data, renders any tiles
    not already cached to disk, and packages everything into a PMTiles
    archive.  This guarantees the export is complete even if the
    pre-render worker hasn't finished yet.
    """
    if style not in VALID_STYLES:
        raise HTTPException(404, f"Unknown style: {style}")

    loop = asyncio.get_event_loop()

    # Block export if pre-rendering is still in progress
    dirty_count = await loop.run_in_executor(None, count_dirty_tiles)
    if dirty_count > 0:
        return JSONResponse({
            "status": "error",
            "message": f"Pre-rendering is still in progress ({dirty_count} tiles remaining). "
                       f"Please wait for pre-rendering to complete before exporting."
        }, status_code=409)

    def _build_pmtiles():
        from pmtiles.writer import Writer as PMTilesWriter
        from pmtiles.tile import zxy_to_tileid, TileType, Compression
        from app.database import get_all_data_tile_coords

        export_dir = Path("/data/export")
        export_dir.mkdir(parents=True, exist_ok=True)
        out_path = export_dir / f"{style}.pmtiles"
        tiles_dir = Path(f"/data/tiles/{style}")
        tiles_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Enumerate ALL tile coords that should contain data
        logger.info(f"PMTiles export ({style}): scanning database for tile coordinates...")
        all_coords = get_all_data_tile_coords()
        logger.info(f"PMTiles export ({style}): found {len(all_coords)} tile coordinates with data")

        # Step 2: Also collect any tiles already on disk (covers tiles that
        # may exist from previous renders even if not in current data scan)
        on_disk = set()
        if tiles_dir.exists():
            for z_dir in tiles_dir.iterdir():
                if not z_dir.is_dir():
                    continue
                try:
                    z = int(z_dir.name)
                except ValueError:
                    continue
                for x_dir in z_dir.iterdir():
                    if not x_dir.is_dir():
                        continue
                    try:
                        x_val = int(x_dir.name)
                    except ValueError:
                        continue
                    for y_file in x_dir.iterdir():
                        if y_file.suffix == ".png":
                            try:
                                y_val = int(y_file.stem)
                                on_disk.add((z, x_val, y_val))
                            except ValueError:
                                continue

        # Merge: export everything in all_coords ∪ on_disk
        all_tiles = all_coords | on_disk

        if not all_tiles:
            return None, "No tiles found for this style", 0

        # Step 3: Render any missing tiles on-the-fly
        missing = all_tiles - on_disk
        rendered_on_fly = 0
        if missing:
            logger.info(
                f"PMTiles export ({style}): {len(missing)} tiles not on disk, "
                f"rendering on-the-fly..."
            )
            for i, (z, x, y) in enumerate(sorted(missing)):
                segments = get_track_segments_in_tile(z, x, y)
                if segments:
                    img = render_tile(segments, z, x, y, style)
                    if img is not None:
                        save_tile(img, style, z, x, y)
                        rendered_on_fly += 1
                if (i + 1) % 500 == 0:
                    logger.info(
                        f"PMTiles export ({style}): rendered {i + 1}/{len(missing)} "
                        f"missing tiles..."
                    )
            logger.info(
                f"PMTiles export ({style}): rendered {rendered_on_fly} missing tiles"
            )

        # Step 4: Build the PMTiles archive from all tiles on disk
        tile_count = 0
        min_zoom = 99
        max_zoom = 0

        with open(out_path, "wb") as f:
            writer = PMTilesWriter(f)

            for z, x, y in sorted(all_tiles):
                tile_path = tiles_dir / str(z) / str(x) / f"{y}.png"
                if not tile_path.exists():
                    continue  # tile had no data (empty render)

                tile_data = tile_path.read_bytes()
                tile_id = zxy_to_tileid(z, x, y)
                writer.write_tile(tile_id, tile_data)
                tile_count += 1
                min_zoom = min(min_zoom, z)
                max_zoom = max(max_zoom, z)

            if tile_count == 0:
                return None, "No tiles found for this style", 0

            # Compute actual data bounds for the header
            from app.database import get_conn as _get_conn
            with _get_conn() as _conn:
                _brow = _conn.execute(
                    """SELECT MIN(bounds_min_lat) as min_lat,
                              MIN(bounds_min_lon) as min_lon,
                              MAX(bounds_max_lat) as max_lat,
                              MAX(bounds_max_lon) as max_lon
                       FROM files"""
                ).fetchone()
            if _brow and _brow["min_lat"] is not None:
                min_lat_e7 = int(_brow["min_lat"] * 1e7)
                min_lon_e7 = int(_brow["min_lon"] * 1e7)
                max_lat_e7 = int(_brow["max_lat"] * 1e7)
                max_lon_e7 = int(_brow["max_lon"] * 1e7)
                center_lon_e7 = (min_lon_e7 + max_lon_e7) // 2
                center_lat_e7 = (min_lat_e7 + max_lat_e7) // 2
            else:
                min_lat_e7, min_lon_e7 = -900000000, -1800000000
                max_lat_e7, max_lon_e7 = 900000000, 1800000000
                center_lon_e7, center_lat_e7 = 0, 0

            header = {
                "tile_type": TileType.PNG,
                "tile_compression": Compression.NONE,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
                "min_lon_e7": min_lon_e7,
                "min_lat_e7": min_lat_e7,
                "max_lon_e7": max_lon_e7,
                "max_lat_e7": max_lat_e7,
                "center_lon_e7": center_lon_e7,
                "center_lat_e7": center_lat_e7,
                "center_zoom": (min_zoom + max_zoom) // 2,
            }
            metadata = {
                "name": f"Local Heatmap ({style})",
                "format": "png",
                "type": "overlay",
            }
            writer.finalize(header, metadata)

        file_size = out_path.stat().st_size
        msg = (
            f"Exported {tile_count} tiles ({file_size / 1024 / 1024:.1f} MB)"
        )
        if rendered_on_fly:
            msg += f" — {rendered_on_fly} tiles rendered on-the-fly"
        return out_path, msg, rendered_on_fly

    out_path, message, rendered_on_fly = await loop.run_in_executor(
        None, _build_pmtiles
    )

    if out_path is None:
        return JSONResponse({"status": "error", "message": message}, status_code=400)

    result = {
        "status": "ok",
        "message": message,
        "download": f"/export/{style}.pmtiles",
    }
    if dirty_count > 0:
        result["warning"] = (
            f"{dirty_count} tiles were still in the pre-render queue. "
            f"Missing tiles were rendered on-the-fly to ensure a complete export."
        )

    return JSONResponse(result)
