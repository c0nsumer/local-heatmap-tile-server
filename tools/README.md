# Tools

Utility scripts for the Local Heatmap Tile Server. These run on the host machine, not inside the Docker container.

## reset.sh

Completely resets the tile server to a clean state. Stops the container, removes the Docker image, and deletes all data (database, tiles, exports, and imported files).

```bash
./tools/reset.sh
```

After running, start fresh with:

```bash
docker compose up -d --build
```

## vpl-to-gpx.sh

Converts Honda/Acura VPLog (.vpl) GPS log files to GPX format using GPSBabel. The resulting GPX files can then be imported into the tile server.

Requires GPSBabel 1.6.0 or older (VPL support was removed in newer versions).

```bash
# Convert all .vpl files in place
./tools/vpl-to-gpx.sh /path/to/vpl/files

# Convert with a separate output directory
./tools/vpl-to-gpx.sh /path/to/vpl/files /path/to/gpx/output

# Specify a custom gpsbabel binary path
./tools/vpl-to-gpx.sh /path/to/vpl/files /path/to/gpx/output /usr/local/bin/gpsbabel-1.6.0
```

Skips files that already have a corresponding .gpx output.
