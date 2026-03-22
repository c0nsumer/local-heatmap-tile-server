# Local Heatmap Tile Server v1

A self-hosted, Docker-based system that imports GPS activity files and generates heatmap XYZ tiles for use with [MapLibre GL JS](https://maplibre.org/projects/gl-js/), [Leaflet](https://leafletjs.com/), [JOSM](https://josm.openstreetmap.de), and many other map clients.

## Note about AI Development

Almost the entirity of this project (but not this paragraph) was built using [Claude](https://claude.ai) to both quickly scratch an itch (this tile server is very useful for me when making maps and in moving away from SaaS fitness trackers) and to become more comfortable and familiar with AI-assisted development, with a significant amount of manual bug testing and iterating through features.

## Tile Server Features

- **Multi-format import**: Supports `.fit`, `.gpx`, and `.tcx` files from Garmin, Wahoo, Karoo, and other devices. Supports GPX files which contain multiple tracks, such as those exported from [rubiTrack](https://www.rubitrack.com/) or [JOSM](https://josm.openstreetmap.de).
- **Large imports**: Capable of importing a large number of files, or tracks, at once. Tested to import 4000+ .FIT files at once, and a single .GPX (exported from rubiTrack) containing 3981 tracks.
- **Three heatmap styles**: Warm (orange/red), Cool (blue), and Top 10% (lime green overlay highlighting most-used routes).
- **XYZ tile endpoints**: Standard `/{style}/{z}/{x}/{y}.png` URLs compatible with any tile client.
- **Incremental updates**: Only tiles affected by new data are re-rendered.
- **Duplicate detection**: Files are SHA256-hashed to prevent re-importing.
- **nginx static serving**: Pre-rendered tiles served directly from disk by nginx for fast loading.

## Live Heatmap Viewer Features

- **MapLibre GL JS viewer**: Built-in WebGL map with automatic light/dark mode, smooth fractional zoom, basemap selection, and bookmarkable URLs.
- **JOSM compatible**: One-click "Open in JOSM" link with style selection.
- **PMTiles export**: Package tiles into a single PMTiles file for static hosting or sharing.
- **PMTiles viewer**: Includes example viewer for static PMTiles file(s) and Caddy configuration for easily serving the files.
- **GPX overlay**: Drag-and-drop GPX files onto the map viewer to compare routes against the heatmap.
- **Data Manager**: Live pre-render progress, file upload, and import/rebuild/export controls.

## Quick Start

```bash
# Start with docker compose
docker compose up -d --build

# Copy activity files into the import directory
cp ~/garmin-exports/*.fit ./data/import/
cp ~/wahoo-exports/*.gpx ./data/import/

# Trigger an import (or use the Data Manager UI)
curl -X POST http://localhost:8000/api/scan

# Open the viewer
open http://localhost:8000/

# Open the Data Manager
open http://localhost:8000/manager
```

## Architecture

The container runs nginx and uvicorn (FastAPI) via supervisord:

- **nginx** (port 8000): Serves pre-rendered tiles directly from disk using `sendfile()`. Falls back to uvicorn for tiles not yet rendered. Proxies all API and UI requests to uvicorn.
- **uvicorn** (port 8001, internal): Handles file imports, on-the-fly tile rendering, the pre-render background worker, and all API endpoints.

## Heatmap Styles

| Style | Description | Usage |
|-------|-------------|-------|
| **Warm** | Orange/red gradient | Base heatmap (default) |
| **Cool** | Blue gradient | Alternative base heatmap |
| **Top 10%** | Lime green, transparent overlay | Toggle on top of warm or cool to highlight most-ridden routes |

The Top 10% layer only shows pixels where track overlap intensity exceeds the 90th percentile. Everything below is fully transparent, so it works as an overlay on either base style.

## Tile URLs

| Style | URL |
|-------|-----|
| Warm (orange/red) | `http://localhost:8000/tiles/warm/{z}/{x}/{y}.png` |
| Cool (blue) | `http://localhost:8000/tiles/cool/{z}/{x}/{y}.png` |
| Top 10% (overlay) | `http://localhost:8000/tiles/top10/{z}/{x}/{y}.png` |

Tiles are rendered at zoom levels 2–18. The viewer smoothly upscales z18 tiles for zoom levels 19–20.

## JOSM Imagery Layer

Click the "Open in JOSM" link in the viewer to add the heatmap as an imagery layer (uses whichever style is currently selected). Or add it manually:

**Imagery → Custom Imagery:**

- URL: `tms:http://localhost:8000/tiles/warm/{z}/{x}/{y}.png`
- Name: `Local Heatmap`
- Min zoom: 2, Max zoom: 18

## API Endpoints

### Tile Serving

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/tiles/{style}/{z}/{x}/{y}.png` | Serve a heatmap tile (`style`: `warm`, `cool`, or `top10`) |

### Import

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/scan` | Scan the import directory for new activity files and import them |
| `POST` | `/api/import` | Upload activity files (.fit, .gpx, .tcx) via multipart form |

### Statistics & Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/stats` | File/track counts, GPS point totals, data bounds |
| `POST` | `/api/rebuild` | Clear all tile caches and queue everything for re-rendering |
| `POST` | `/api/rebuild/{style}` | Clear cache for a specific style and queue for re-rendering |

### Pre-render Worker

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/prerender/status` | Worker state, batch progress, tiles remaining |
| `POST` | `/api/prerender/pause` | Pause background pre-rendering |
| `POST` | `/api/prerender/resume` | Resume background pre-rendering |

### PMTiles Export

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/export/pmtiles?style=warm` | Export tiles as a PMTiles archive (default: warm) |
| `GET` | `/export/{filename}` | Download an exported PMTiles file |

### Web UI

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | MapLibre GL JS map viewer with GPX overlay support |
| `GET` | `/manager` | Data Manager with upload, import, rebuild, and export controls |

## Import Workflow

1. Upload files via the Data Manager UI, or copy them into `./data/import/`.
2. If copied to the import directory, trigger a scan via the Data Manager or: `curl -X POST http://localhost:8000/api/scan`.
3. Files are parsed, deduplicated by SHA256 hash, and stored in SQLite.
4. Successfully imported files are moved to `./data/import/done/`.
5. Files that fail to parse are moved to `./data/import/errors/`.
6. Affected tiles are automatically marked dirty and queued for pre-rendering.

Multi-track GPX files are automatically split into individual tracks on import.

## Pre-rendering

After importing, affected tiles are automatically queued for background pre-rendering.
The worker renders tiles in parallel batches, producing all three styles (warm, cool, top10) for each tile.

```bash
# Check pre-render progress
curl -s http://localhost:8000/api/prerender/status

# Force a full re-render of all tiles
curl -X POST http://localhost:8000/api/rebuild
```

## PMTiles Export

Export heatmap tiles as a single PMTiles file for static hosting or sharing:

```bash
# Export a specific style
curl -X POST http://localhost:8000/api/export/pmtiles?style=warm

# Download the exported file
curl -O http://localhost:8000/export/warm.pmtiles
```

See `PMTiles Viewer/` for a standalone viewer and Caddy configuration for hosting the exported files.

## GPX Overlay

The map viewer supports loading GPX files as overlays to compare planned or recorded routes against the heatmap:

- **Drag and drop** a `.gpx` file onto the map or the drop zone in the panel.
- **Click** the "GPX Overlay" area to browse for files.
- Routes are rendered as white lines with a dark outline for visibility over all heatmap styles and basemaps.
- Multiple overlays can be loaded simultaneously; each can be individually removed.
- Files are parsed client-side: nothing is uploaded to the server.

## Configuration

Environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TILE_MIN_ZOOM` | `2` | Minimum zoom level to render |
| `TILE_MAX_ZOOM` | `18` | Maximum zoom level to render |
| `LINE_WIDTH` | `2.0` | GPS track line width in pixels |
| `TILE_SIZE` | `256` | Tile dimensions in pixels |
| `PRERENDER_BATCH_SIZE` | `50` | Tiles to render per batch |
| `PRERENDER_BATCH_PAUSE` | `0.5` | Seconds to pause between batches |
| `PRERENDER_IDLE_PAUSE` | `5.0` | Seconds to sleep when queue is empty |
| `PRERENDER_WORKERS` | CPU count | Parallel render worker threads |
| `MAX_SEGMENTS_PER_TILE` | `2500` | Max track segments per tile (caps memory on low-zoom tiles) |
| `MAX_POINTS_PER_TILE` | `200000` | Max trackpoints queried per tile |
| `TILE_MEM_CACHE_SIZE` | `2000` | In-memory LRU cache size (number of tiles) |

## Directory Layout

```
./data/
  import/         Place activity files here (.fit, .gpx, .tcx)
    done/         Successfully imported files
    errors/       Files that failed to parse
  db/             SQLite database (heatmap.db)
  tiles/          Rendered tile cache
    warm/         Orange/red heatmap tiles
    cool/         Blue heatmap tiles
    top10/        Top 10% overlay tiles (lime green)
  export/         PMTiles exports
```

## Dependencies

**Server (Docker container):**
- Python 3.12, FastAPI, uvicorn
- Pillow + NumPy (tile rendering)
- fitdecode (FIT file parsing)
- gpxpy (GPX file parsing)
- pmtiles (PMTiles export)
- nginx (static tile serving)
- supervisor (process management)

**Client (loaded from CDN by the browser):**
- [MapLibre GL JS](https://maplibre.org/projects/gl-js/) (WebGL map rendering)
- [toGeoJSON](https://github.com/mapbox/togeojson) (client-side GPX parsing)

# Additional Tools

## PMTiles Viewer

The `PMTiles Viewer/` directory contains a standalone MapLibre GL JS viewer and Caddy configuration for hosting exported PMTiles files without the tile server. Uses the native `pmtiles://` protocol for efficient tile loading. See `PMTiles Viewer/README.md` for setup instructions.

## Tools

The `tools/` directory contains utility scripts that run on the host:

- **`reset.sh`**: Completely resets the server: stops the container, removes the image, and deletes all data
- **`vpl-to-gpx.sh`**: Converts Honda/Acura VPLog (.vpl) GPS files to GPX using GPSBabel

See `tools/README.md` for details.

## Author

Steve Vigneau / [nuxx.net](https://nuxx.net) / <steve@nuxx.net>

Built with [Claude Code](https://claude.com/claude-code) (Claude Opus 4.6, Anthropic).

## License

[MIT](LICENSE)
