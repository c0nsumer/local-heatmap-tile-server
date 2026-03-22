#!/usr/bin/env bash
# Convert all .vpl (VPLog) files in a directory to .gpx using GPSBabel.
# Usage: ./vpl-to-gpx.sh [input_dir] [output_dir] [gpsbabel_path]
#   input_dir     — directory containing .vpl files (default: current directory)
#   output_dir    — where to write .gpx files (default: same as input_dir)
#   gpsbabel_path — path to gpsbabel binary (default: gpsbabel on PATH)

set -euo pipefail

INPUT_DIR="${1:-.}"
OUTPUT_DIR="${2:-$INPUT_DIR}"
GPSBABEL="${3:-gpsbabel}"

if ! command -v "$GPSBABEL" &>/dev/null && [[ ! -x "$GPSBABEL" ]]; then
    echo "Error: gpsbabel not found at '$GPSBABEL'."
    echo "  Provide the path as the third argument, or install it:"
    echo "  macOS:   brew install gpsbabel"
    echo "  Debian:  sudo apt install gpsbabel"
    echo "Note: VPL support was removed in newer versions. You may need GPSBabel 1.6.0 or older."
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

count=0
errors=0

shopt -s nullglob nocaseglob
for vpl in "$INPUT_DIR"/*.vpl; do
    base="$(basename "${vpl%.*}")"
    gpx="$OUTPUT_DIR/${base}.gpx"

    if [[ -f "$gpx" ]]; then
        echo "SKIP  $base.vpl (gpx already exists)"
        continue
    fi

    if "$GPSBABEL" -t -i vpl -f "$vpl" -o gpx -F "$gpx" 2>/dev/null; then
        echo "OK    $base.vpl -> $base.gpx"
        ((count++)) || true
    else
        echo "FAIL  $base.vpl"
        rm -f "$gpx"
        ((errors++)) || true
    fi
done

echo ""
echo "Done. Converted: $count  Errors: $errors"
