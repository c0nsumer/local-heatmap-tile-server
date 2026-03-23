"""Render heatmap tiles from GPS trackpoints using Pillow and NumPy."""

import math
import threading
from collections import OrderedDict
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from pathlib import Path
import os
import logging

logger = logging.getLogger(__name__)

TILE_SIZE = int(os.environ.get("TILE_SIZE", "256"))
LINE_WIDTH = float(os.environ.get("LINE_WIDTH", "2.0"))
TILES_DIR = Path("/data/tiles")
MAX_SEGMENTS_PER_TILE = int(os.environ.get("MAX_SEGMENTS_PER_TILE", "1000"))

# Color ramps: (threshold, r, g, b) — alpha is computed from intensity
COLOR_RAMPS = {
    "warm": [
        (0.0,   0,   0,   0),
        (0.05,  40,   5,   0),
        (0.15,  80,  15,   0),
        (0.3,  160,  30,   0),
        (0.5,  220,  70,   5),
        (0.75, 255, 120,  20),
        (1.0,  200,  60,   5),
    ],
    "cool": [
        (0.0,   0,   0,   0),
        (0.05,   0,   5,  40),
        (0.15,   0,  15,  80),
        (0.3,    0,  40, 160),
        (0.5,   10,  80, 220),
        (0.75,  30, 140, 255),
        (1.0,   10,  60, 200),
    ],
    "top10": [
        # Transparent below 90% — this is an overlay layer
        (0.0,    0,   0,   0),
        (0.89,   0,   0,   0),
        # Top 10%: lime green highlight
        (0.90,   0, 200,  80),
        (0.95,   0, 255, 136),
        (1.0,   68, 255, 170),
    ],
}

VALID_STYLES = set(COLOR_RAMPS.keys())


def build_color_lut(ramp: list, size: int = 256) -> np.ndarray:
    """Pre-build a 256-entry RGBA lookup table from a color ramp.

    Returns an (size, 4) uint8 array mapping intensity index → RGBA.
    Index 0 is always fully transparent.
    """
    lut = np.zeros((size, 4), dtype=np.uint8)

    for idx in range(1, size):
        t = idx / (size - 1)
        # Find surrounding breakpoints
        r, g, b = 0, 0, 0
        for i in range(len(ramp) - 1):
            t0, r0, g0, b0 = ramp[i]
            t1, r1, g1, b1 = ramp[i + 1]
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
                r = int(r0 + (r1 - r0) * frac)
                g = int(g0 + (g1 - g0) * frac)
                b = int(b0 + (b1 - b0) * frac)
                break
        else:
            _, r, g, b = ramp[-1]

        # Alpha: if the color is black (r=g=b=0), keep fully transparent.
        # Otherwise ramp alpha based on intensity.
        if r == 0 and g == 0 and b == 0:
            a = 0
        else:
            a = int(min(255, t * 3 * 255))
        lut[idx] = (r, g, b, a)

    return lut


# Pre-build LUTs for each style at import time
_COLOR_LUTS: dict[str, np.ndarray] = {}


def _get_lut(style: str = "warm") -> np.ndarray:
    """Get (or lazily build) the color LUT for a style."""
    if style not in _COLOR_LUTS:
        _COLOR_LUTS[style] = build_color_lut(COLOR_RAMPS[style])
    return _COLOR_LUTS[style]


def _batch_latlon_to_pixels(points: np.ndarray, z: int, tile_x: int,
                            tile_y: int) -> np.ndarray:
    """Vectorized lat/lon to tile pixel conversion.

    Args:
        points: (N, 2) array of (lat, lon) pairs
        z, tile_x, tile_y: Tile coordinates

    Returns:
        (N, 2) array of (px, py) pixel coordinates within the tile
    """
    n = 2 ** z
    lats = points[:, 0]
    lons = points[:, 1]

    px = (lons + 180.0) / 360.0 * n * TILE_SIZE - tile_x * TILE_SIZE
    lat_rad = np.radians(lats)
    py = ((1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad))
           / math.pi) / 2.0 * n * TILE_SIZE) - tile_y * TILE_SIZE

    return np.column_stack((px, py))


def render_tile(segments: list[list[tuple[float, float]]],
                z: int, x: int, y: int,
                style: str = "warm") -> Image.Image | None:
    """
    Render a single heatmap tile from track segments.

    Args:
        segments: List of tracks, each a list of (lat, lon) points
        z, x, y: Tile coordinates
        style: Color style ("warm" or "cool")

    Returns:
        PIL Image (RGBA) or None if no data
    """
    if not segments:
        return None

    # Cap segments to avoid unbounded memory on low-zoom tiles.
    # At low zoom, a 256px tile can't visually distinguish thousands of
    # overlapping 1px lines, so a lower cap saves memory with no visual cost.
    if z <= 4:
        effective_cap = min(MAX_SEGMENTS_PER_TILE, 500)
    elif z <= 8:
        effective_cap = min(MAX_SEGMENTS_PER_TILE, 2000)
    else:
        effective_cap = MAX_SEGMENTS_PER_TILE

    if len(segments) > effective_cap:
        logger.warning(
            f"Tile {style}/{z}/{x}/{y}: capping {len(segments)} segments "
            f"to {effective_cap}"
        )
        segments = segments[:effective_cap]

    # Determine line width based on zoom level
    base_width = LINE_WIDTH
    if z <= 4:
        width = 1.0
    elif z <= 8:
        width = max(1.0, base_width * 0.5)
    elif z <= 11:
        width = base_width
    elif z <= 13:
        width = base_width * 1.5
    else:
        width = base_width * 2.0
    int_width = max(1, int(width))

    margin = TILE_SIZE * 0.5

    # Maximum geographic distance (in km) between consecutive GPS points
    # before we consider it a jump (driving between activities, GPS glitch,
    # etc.) rather than actual movement.  Normal GPS recordings produce
    # points every 1-10 seconds, so even at highway speed consecutive
    # points are well under 1km apart.
    MAX_GAP_KM = 1.0

    # Each track gets its own temporary grayscale image so overlapping
    # tracks accumulate intensity (each track contributes at most 1.0).
    buf = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.float32)

    for track in segments:
        if len(track) < 2:
            continue

        # Vectorized coordinate conversion
        pts = np.array(track, dtype=np.float64)
        pixels = _batch_latlon_to_pixels(pts, z, x, y)

        # Vectorized segment filtering
        x0s, y0s = pixels[:-1, 0], pixels[:-1, 1]
        x1s, y1s = pixels[1:, 0], pixels[1:, 1]

        # Skip segments entirely outside tile margin
        seg_max_x = np.maximum(x0s, x1s)
        seg_min_x = np.minimum(x0s, x1s)
        seg_max_y = np.maximum(y0s, y1s)
        seg_min_y = np.minimum(y0s, y1s)
        in_bounds = ((seg_max_x >= -margin) & (seg_min_x <= TILE_SIZE + margin) &
                     (seg_max_y >= -margin) & (seg_min_y <= TILE_SIZE + margin))

        # Compute geographic distance between consecutive points (in km)
        # using equirectangular approximation — fast and accurate enough
        # for detecting jumps.
        lats0, lons0 = pts[:-1, 0], pts[:-1, 1]
        lats1, lons1 = pts[1:, 0], pts[1:, 1]
        avg_lat_rad = np.radians((lats0 + lats1) / 2.0)
        dlat = np.radians(lats1 - lats0)
        dlon = np.radians(lons1 - lons0) * np.cos(avg_lat_rad)
        geo_dist_km = 6371.0 * np.sqrt(dlat**2 + dlon**2)

        valid = in_bounds & (geo_dist_km <= MAX_GAP_KM)

        if not np.any(valid):
            continue

        # Draw valid segments onto a temp image for this track
        tmp = Image.new("L", (TILE_SIZE, TILE_SIZE), 0)
        draw = ImageDraw.Draw(tmp)

        valid_indices = np.where(valid)[0]
        for i in valid_indices:
            draw.line([(x0s[i], y0s[i]), (x1s[i], y1s[i])],
                      fill=255, width=int_width)

        buf += np.array(tmp, dtype=np.float32) * (1.0 / 255.0)

    # Check if there's any data
    if buf.max() == 0:
        return None

    # Apply Gaussian blur for subtle glow — minimal at all zoom levels
    if z <= 8:
        blur_radius = 0.6
    elif z <= 11:
        blur_radius = 0.4
    else:
        blur_radius = 0

    # Normalize using log scale with a zoom-dependent fixed cap.
    # At low zoom, many traces overlap per pixel so the cap is higher.
    # At high zoom, traces rarely overlap so the cap is lower.
    # Using log1p(cap) since we normalize after the log transform.
    if z <= 4:
        norm_cap = np.log1p(5)
    elif z <= 8:
        norm_cap = np.log1p(5)
    elif z <= 11:
        norm_cap = np.log1p(15)
    elif z <= 14:
        norm_cap = np.log1p(10)
    else:
        norm_cap = np.log1p(5)

    buf = np.where(buf > 0, np.log1p(buf), 0)
    buf = buf / norm_cap
    buf = np.clip(buf, 0, 1.0)

    # Convert intensity buffer to RGBA using pre-built color LUT
    lut = _get_lut(style)
    indices = (buf * 255).astype(np.uint8)
    rgba = lut[indices]

    img = Image.fromarray(rgba, "RGBA")

    if blur_radius > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    return img


def get_tile_path(style: str, z: int, x: int, y: int) -> Path:
    """Get the filesystem path for a cached tile."""
    return TILES_DIR / style / str(z) / str(x) / f"{y}.png"


def save_tile(img: Image.Image, style: str, z: int, x: int, y: int):
    """Save a rendered tile to disk cache and populate memory cache."""
    path = get_tile_path(style, z, x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG")
    # Also store in memory cache
    tile_bytes = path.read_bytes()
    _cache_put((style, z, x, y), tile_bytes)


# In-memory LRU cache for tile bytes — avoids disk reads for hot tiles.
# Thread-safe via lock; O(1) operations via OrderedDict.
# Sized to hold ~2000 tiles (~10-20MB depending on tile complexity).
_TILE_CACHE_MAX = int(os.environ.get("TILE_MEM_CACHE_SIZE", "2000"))
_tile_mem_cache: OrderedDict[tuple, bytes] = OrderedDict()
_tile_cache_lock = threading.Lock()


def _cache_put(key: tuple, data: bytes):
    """Add a tile to the memory cache, evicting oldest if full."""
    with _tile_cache_lock:
        if key in _tile_mem_cache:
            _tile_mem_cache.move_to_end(key)
        else:
            if len(_tile_mem_cache) >= _TILE_CACHE_MAX:
                _tile_mem_cache.popitem(last=False)
        _tile_mem_cache[key] = data


def _cache_remove(key: tuple):
    """Remove a tile from the memory cache if present."""
    with _tile_cache_lock:
        _tile_mem_cache.pop(key, None)


def load_cached_tile(style: str, z: int, x: int, y: int) -> bytes | None:
    """Load a cached tile, checking memory first, then disk."""
    key = (style, z, x, y)

    # Check memory cache
    with _tile_cache_lock:
        data = _tile_mem_cache.get(key)
        if data is not None:
            _tile_mem_cache.move_to_end(key)
            return data

    # Fall back to disk
    path = get_tile_path(style, z, x, y)
    if path.exists():
        data = path.read_bytes()
        _cache_put(key, data)
        return data
    return None


def clear_tile_cache(style: str | None = None):
    """Clear tile cache (disk and memory) for a style or all."""
    import shutil
    # Clear memory cache
    with _tile_cache_lock:
        if style:
            keys_to_remove = [k for k in _tile_mem_cache if k[0] == style]
            for k in keys_to_remove:
                del _tile_mem_cache[k]
        else:
            _tile_mem_cache.clear()
    if style:
        p = TILES_DIR / style
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    else:
        if TILES_DIR.exists():
            for child in TILES_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
