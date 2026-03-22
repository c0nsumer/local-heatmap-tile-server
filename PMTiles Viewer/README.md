# PMTiles Viewer

A standalone MapLibre GL JS map viewer that reads heatmap tiles from PMTiles archives, served by Caddy. Use this to host your exported heatmap without running the tile server.

Uses MapLibre's native `pmtiles://` protocol for efficient tile loading with smooth fractional zoom, fluid pinch/scroll zooming, and WebGL-accelerated rendering.

## Setup

### 1. Export PMTiles from the tile server

```bash
curl -X POST http://localhost:8000/api/export/pmtiles?style=warm
curl -X POST http://localhost:8000/api/export/pmtiles?style=cool
curl -X POST http://localhost:8000/api/export/pmtiles?style=top10
```

Or use the "Export PMTiles" button in the Data Manager at `http://localhost:8000/manager`.

### 2. Prepare the hosting directory

```bash
mkdir -p /srv/heatmap
cp /path/to/data/export/*.pmtiles /srv/heatmap/
cp pmtiles-viewer.html /srv/heatmap/index.html
```

### 3. Configure the viewer

The default `PMTILES_BASE = '.'` loads `.pmtiles` files from the same directory as the HTML file, so no configuration is needed if they're together. To load from a different location, edit the `PMTILES_BASE` variable in `pmtiles-viewer.html`:

```javascript
// Same directory as the HTML file (default):
var PMTILES_BASE = '.';

// Remote server:
var PMTILES_BASE = 'https://tiles.example.com';
```

### 4. Start Caddy

```bash
caddy run --config Caddyfile
```

### 5. Open the viewer

```
http://localhost:8080
```

## Files

| File | Description |
|------|-------------|
| `pmtiles-viewer.html` | Standalone MapLibre GL JS viewer with PMTiles support |
| `Caddyfile` | Caddy web server configuration |

## Viewer Features

- **Smooth WebGL rendering** — Fractional zoom, fluid pinch/scroll, GPU-accelerated tile compositing
- **Heatmap style selector** — Switch between Warm (orange/red) and Cool (blue)
- **Top 10% overlay** — Toggle a lime green overlay highlighting the most-used routes
- **Basemap picker** — CartoDB Dark, OpenStreetMap, or CartoDB Positron
- **GPX overlay** — Drag-and-drop GPX files to display routes as white lines on top of the heatmap
- **Bookmarkable URLs** — Map position, zoom level, style, and overlay state stored in the URL hash

## Caddy Configuration

The included `Caddyfile` serves files from `/srv/heatmap` on port 8080. It adds CORS and range request headers for PMTiles files, which are required for the browser-based PMTiles reader to fetch individual tiles via HTTP range requests.

Edit the port or root directory as needed for your setup.

## Requirements

- [Caddy](https://caddyserver.com/) (or any web server that supports HTTP range requests)
- Exported `.pmtiles` files from the tile server
- A modern browser with WebGL support
