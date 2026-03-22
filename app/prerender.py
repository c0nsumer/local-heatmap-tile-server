"""Background worker that pre-renders dirty tiles."""

import asyncio
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor

from app.database import (
    get_dirty_tiles, clear_dirty_tile, count_dirty_tiles,
    get_track_segments_in_tile
)
from app.renderer import render_tile, save_tile, get_tile_path, VALID_STYLES

NUM_STYLES = len(VALID_STYLES)

logger = logging.getLogger(__name__)

# How many tiles to render per batch before yielding
BATCH_SIZE = int(os.environ.get("PRERENDER_BATCH_SIZE", "50"))

# Seconds to sleep between batches (keeps CPU available for live requests)
BATCH_PAUSE = float(os.environ.get("PRERENDER_BATCH_PAUSE", "0.5"))

# Seconds to sleep when the dirty queue is empty
IDLE_PAUSE = float(os.environ.get("PRERENDER_IDLE_PAUSE", "5.0"))

# Number of parallel render workers (defaults to CPU count, min 1)
RENDER_WORKERS = int(os.environ.get("PRERENDER_WORKERS", "0")) or (os.cpu_count() or 2)

# Track rendering state for the status API
_status = {
    "state": "idle",          # idle | rendering | paused
    "rendered_total": 0,      # tiles rendered since startup
    "rendered_session": 0,    # tiles rendered in current batch run
    "remaining": 0,           # dirty tiles left
    "last_render_time": None, # timestamp of last rendered tile
    "enabled": True,          # can be paused via API
}


def get_prerender_status() -> dict:
    """Return a copy of the current pre-render status."""
    _status["remaining"] = count_dirty_tiles()
    return dict(_status)


def set_prerender_enabled(enabled: bool):
    _status["enabled"] = enabled
    _status["state"] = "idle" if enabled else "paused"


def _render_one_tile(z: int, x: int, y: int) -> bool:
    """Render and cache a single tile in all styles.

    Returns True if at least one style produced a tile.
    """
    segments = get_track_segments_in_tile(z, x, y)

    if not segments:
        clear_dirty_tile(z, x, y)
        return False

    produced = False
    for style in VALID_STYLES:
        img = render_tile(segments, z, x, y, style)
        if img is not None:
            save_tile(img, style, z, x, y)
            produced = True

    clear_dirty_tile(z, x, y)
    return produced


# Module-level thread pool (created lazily)
_pool: ThreadPoolExecutor | None = None


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=RENDER_WORKERS)
    return _pool


async def prerender_worker():
    """Background async loop that processes the dirty tile queue.

    Renders tiles in parallel using a ThreadPoolExecutor. Threads share
    memory (unlike processes), and NumPy/Pillow release the GIL during
    computation so we still get real parallelism.
    """
    pool = _get_pool()
    logger.info(
        f"Pre-render worker started (batch={BATCH_SIZE}, "
        f"pause={BATCH_PAUSE}s, idle={IDLE_PAUSE}s, "
        f"workers={RENDER_WORKERS})"
    )

    loop = asyncio.get_event_loop()

    while True:
        try:
            if not _status["enabled"]:
                _status["state"] = "paused"
                await asyncio.sleep(IDLE_PAUSE)
                continue

            # Fetch a batch of dirty tiles
            dirty = await loop.run_in_executor(
                None, get_dirty_tiles, BATCH_SIZE
            )

            if not dirty:
                _status["state"] = "idle"
                _status["rendered_session"] = 0
                await asyncio.sleep(IDLE_PAUSE)
                continue

            _status["state"] = "rendering"

            # Submit entire batch to the thread pool in parallel.
            # Per-tile data caps (MAX_POINTS_PER_TILE, MAX_SEGMENTS_PER_TILE)
            # keep individual tile memory bounded.
            rendered_in_batch = 0
            futures = []
            for z, x, y in dirty:
                fut = loop.run_in_executor(
                    pool, _render_one_tile, z, x, y
                )
                futures.append((z, x, y, fut))

            for z, x, y, fut in futures:
                try:
                    produced = await fut
                    if produced:
                        rendered_in_batch += 1
                        _status["rendered_total"] += 1
                        _status["rendered_session"] += 1
                        _status["last_render_time"] = time.time()
                except Exception as e:
                    # Don't clear the dirty flag — leave it so the tile
                    # gets retried in a future batch.
                    logger.warning(
                        f"Pre-render failed for {z}/{x}/{y}: {e}"
                    )

            remaining = count_dirty_tiles()
            _status["remaining"] = remaining

            if rendered_in_batch > 0:
                logger.info(
                    f"Pre-rendered {rendered_in_batch} tiles "
                    f"\u00d7 {NUM_STYLES} styles "
                    f"({rendered_in_batch * NUM_STYLES} images) "
                    f"\u2014 {remaining} tiles remaining "
                    f"({remaining * NUM_STYLES} images)"
                )

            # Yield to let live requests through
            await asyncio.sleep(BATCH_PAUSE)

        except asyncio.CancelledError:
            logger.info("Pre-render worker shutting down")
            raise
        except Exception as e:
            logger.error(f"Pre-render worker error: {e}")
            await asyncio.sleep(IDLE_PAUSE)
