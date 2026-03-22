#!/bin/bash
# Reset Local Heatmap Tile Server to a completely clean state.
# Stops the container, removes the image, and wipes all data
# including imported files, tiles, and the database.

set -e

# Always operate from the project root (one level up from tools/)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "Stopping container and removing image..."
docker compose down --rmi local

echo "Removing database..."
rm -rf ./data/db

echo "Removing tile cache..."
rm -rf ./data/tiles

echo "Removing PMTiles exports..."
rm -rf ./data/export

echo "Removing all imported and queued files..."
rm -rf ./data/import/done
rm -rf ./data/import/errors
rm -f ./data/import/*.fit ./data/import/*.FIT
rm -f ./data/import/*.gpx ./data/import/*.GPX
rm -f ./data/import/*.tcx ./data/import/*.TCX

echo "Reset complete. Run 'docker compose up -d --build' to start fresh."
