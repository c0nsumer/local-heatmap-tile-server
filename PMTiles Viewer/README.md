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
| `pmtiles-viewer.html` | Full-featured viewer with collapsible controls, light/dark mode, style selector, Top 10% overlay, GPX overlay |
| `pmtiles-viewer-simple.html` | Minimal single-heatmap viewer: just the map, basemap picker, and GPX drag-and-drop |
| `Caddyfile` | Caddy web server configuration |

## Viewer Variants

### Full viewer (`pmtiles-viewer.html`)

- **Light/dark mode**: Follows the OS appearance setting by default, with a manual override (Auto/Light/Dark).
- **Collapsible controls**: Panel starts collapsed to maximize map space; click "Controls" to expand.
- **Auto-centering**: Reads data bounds from the PMTiles header and fits the map on load (unless a bookmarked URL hash is present).
- **Heatmap style selector**: Switch between Warm (orange/red) and Cool (blue).
- **Top 10% overlay**: Toggle a lime green overlay highlighting the most-used routes.
- **Basemap picker**: Auto (follows appearance mode), Dark, Light, or OpenStreetMap.
- **GPX overlay**: Drag-and-drop GPX files with file list and remove buttons.
- **Bookmarkable URLs**: Map position, zoom level, style, and overlay state stored in the URL hash.

### Simple viewer (`pmtiles-viewer-simple.html`)

- **Single heatmap**: Displays one PMTiles file (default: `warm.pmtiles`).
- **Auto-centering**: Reads data bounds from the PMTiles header and fits the map on load.
- **Basemap picker**: Auto (follows OS), Dark, Light, or OSM.
- **GPX drag-and-drop**: Drop GPX files anywhere on the map.
- **Bookmarkable URLs**: Map position and zoom level in URL hash.

To change which PMTiles file it loads, edit the `PMTILES_URL` variable:

```javascript
var PMTILES_URL = './warm.pmtiles';   // default
var PMTILES_URL = './cool.pmtiles';   // blue heatmap
```

## Common Features

- **Smooth WebGL rendering**: Fractional zoom, fluid pinch/scroll, GPU-accelerated tile compositing

## Caddy Configuration

The included `Caddyfile` serves files from `/srv/heatmap` on port 8080. It adds CORS and range request headers for PMTiles files, which are required for the browser-based PMTiles reader to fetch individual tiles via HTTP range requests.

Edit the port or root directory as needed for your setup.

## Requirements

- [Caddy](https://caddyserver.com/) (or any web server that supports HTTP range requests)
- Exported `.pmtiles` files from the tile server
- A modern browser with WebGL support
