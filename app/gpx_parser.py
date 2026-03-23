"""Parse GPX files to extract GPS tracks.

Uses a chunked streaming approach to handle arbitrarily large files
without loading the entire document into memory. Each <trk> block is
extracted as text and parsed independently, so malformed XML in one
track only skips that track — the rest are still imported.
"""

import io
import logging
import re
from pathlib import Path
from xml.etree.ElementTree import iterparse, fromstring, ParseError

logger = logging.getLogger(__name__)


def _strip_ns(tag: str) -> str:
    """Strip namespace prefix from an XML tag."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _sanitize_text_elements(xml: str) -> str:
    """Escape invalid XML characters inside GPX free-text elements.

    GPX files exported by some apps (e.g. rubiTrack) may contain
    unescaped characters in <name>, <desc>, or <cmt> text — things
    like "<angryface>" or "R&R" typed by the user. These are invalid
    XML and cause parse errors.

    Per the GPX spec, <name>, <desc>, and <cmt> contain only plain
    text (no child elements), so we can safely escape all angle
    brackets and bare ampersands within their content.
    """
    def _escape_content(m):
        open_tag = m.group(1)
        content = m.group(2)
        close_tag = m.group(3)
        # Un-escape any existing entities first to avoid double-escaping,
        # then re-escape everything uniformly
        content = content.replace('&amp;', '&')
        content = content.replace('&lt;', '<')
        content = content.replace('&gt;', '>')
        content = content.replace('&quot;', '"')
        content = content.replace('&apos;', "'")
        content = content.replace('&', '&amp;')
        content = content.replace('<', '&lt;')
        content = content.replace('>', '&gt;')
        return open_tag + content + close_tag

    # Match <name>...</name>, <desc>...</desc>, and <cmt>...</cmt>
    # with optional namespace prefix
    return re.sub(
        r'(<(?:\w+:)?(?:name|desc|cmt)>)(.*?)(</(?:\w+:)?(?:name|desc|cmt)>)',
        _escape_content,
        xml,
        flags=re.DOTALL
    )


# Sentinel returned by _parse_trk_block when the XML is valid but has no points
_EMPTY_TRACK = "empty"


def _parse_trk_block(trk_xml: str, filepath_name: str,
                      track_index: int,
                      ns_attrs: str = "") -> dict | str | None:
    """Parse a single <trk>...</trk> XML block into a track dict.

    ns_attrs contains namespace declarations extracted from the root <gpx>
    element (e.g. 'xmlns:gpxdata="..."'), so prefixed elements inside the
    track block can be resolved.

    Returns:
        dict  — successfully parsed track with GPS points
        _EMPTY_TRACK — valid XML but no GPS points
        None  — malformed XML that could not be parsed
    """
    # Sanitize text elements to handle unescaped characters
    trk_xml = _sanitize_text_elements(trk_xml)

    # Wrap in a root element that carries the namespace declarations
    wrapped = f'<?xml version="1.0"?><root {ns_attrs}>{trk_xml}</root>'

    try:
        root = fromstring(wrapped)
    except ParseError as e:
        # Extract track name from raw XML for the log message
        name_match = re.search(r'<name>(.*?)</name>', trk_xml)
        name_hint = f" ({name_match.group(1)})" if name_match else ""
        logger.warning(
            f"Skipping malformed track #{track_index + 1}{name_hint} in "
            f"{filepath_name}: {e}"
        )
        return None

    points = []
    first_time = None
    last_time = None
    track_name = None

    for elem in root.iter():
        tag = _strip_ns(elem.tag)

        if tag == "name" and track_name is None:
            # First <name> in the track (not inside a trkpt)
            track_name = elem.text

    # Now parse trackpoints
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "trkpt":
            try:
                lat = float(elem.get("lat", ""))
                lon = float(elem.get("lon", ""))
            except (ValueError, TypeError):
                continue

            if -90 <= lat <= 90 and -180 <= lon <= 180:
                points.append((lat, lon))

                # Look for <time> child
                for child in elem:
                    if _strip_ns(child.tag) == "time" and child.text:
                        t = child.text.strip()
                        if t:
                            if first_time is None:
                                first_time = t
                            last_time = t
                        break

    if not points:
        return _EMPTY_TRACK

    return {
        "points": points,
        "start_time": first_time,
        "end_time": last_time,
        "track_name": track_name,
        "filename": filepath_name,
        "error": None,
    }


def parse_gpx_file_streaming(filepath: str | Path):
    """
    Parse a .GPX file and yield one track dict at a time.

    Reads the file as text, extracts each <trk>...</trk> block, and
    parses them independently. This approach:
    - Handles arbitrarily large files (reads line by line)
    - Recovers from malformed XML (bad tracks are skipped, not fatal)
    - Frees each track's memory before parsing the next

    Yields:
        dict with:
        {
            "points": [(lat, lon), ...],
            "start_time": str (ISO 8601) | None,
            "end_time": str (ISO 8601) | None,
            "filename": str,
            "track_name": str | None,
            "error": None
        }
    """
    filepath = Path(filepath)

    # Regex patterns to detect <trk> and </trk> tags (with optional namespace)
    trk_open_re = re.compile(r'<(?:\w+:)?trk[\s>/]', re.IGNORECASE)
    trk_close_re = re.compile(r'</(?:\w+:)?trk\s*>', re.IGNORECASE)

    # Regex to capture the <gpx ...> opening tag (possibly spanning lines)
    # We'll extract xmlns:* attributes to pass to each track block
    gpx_open_re = re.compile(r'<(?:\w+:)?gpx\s', re.IGNORECASE)
    ns_attr_re = re.compile(
        r'''(xmlns(?::\w+)?="[^"]*"|xsi:\w+="[^"]*")''')

    in_trk = False
    in_gpx_header = False
    trk_lines = []
    gpx_header_lines = []
    ns_attrs = ""
    track_index = 0
    empty_count = 0
    malformed_count = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Before the first track, capture the <gpx> tag's namespaces
            if not ns_attrs and not in_trk:
                if not in_gpx_header:
                    if gpx_open_re.search(line):
                        in_gpx_header = True
                        gpx_header_lines = [line]
                        # Check if the tag closes on this line
                        if ">" in line[line.index("<gpx") + 4
                                       if "<gpx" in line.lower()
                                       else 0:]:
                            header_text = line
                            in_gpx_header = False
                            ns_attrs = " ".join(
                                ns_attr_re.findall(header_text))
                else:
                    gpx_header_lines.append(line)
                    if ">" in line:
                        header_text = " ".join(gpx_header_lines)
                        in_gpx_header = False
                        ns_attrs = " ".join(
                            ns_attr_re.findall(header_text))

            if not in_trk:
                # Look for a <trk> opening tag
                match = trk_open_re.search(line)
                if match:
                    in_trk = True
                    # Keep from the <trk> tag onward
                    trk_lines = [line[match.start():]]

                    # Check if </trk> is also on this same line
                    close_match = trk_close_re.search(line, match.end())
                    if close_match:
                        # Complete track on one line
                        trk_xml = line[match.start():close_match.end()]
                        in_trk = False
                        trk_lines = []

                        result = _parse_trk_block(
                            trk_xml, filepath.name, track_index,
                            ns_attrs
                        )
                        if result is _EMPTY_TRACK:
                            empty_count += 1
                        elif result is None:
                            malformed_count += 1
                        else:
                            yield result
                        track_index += 1
            else:
                # Inside a <trk> block — accumulate lines
                close_match = trk_close_re.search(line)
                if close_match:
                    # Include up to and including </trk>
                    trk_lines.append(line[:close_match.end()])
                    trk_xml = "".join(trk_lines)
                    in_trk = False
                    trk_lines = []

                    result = _parse_trk_block(
                        trk_xml, filepath.name, track_index,
                        ns_attrs
                    )
                    if result is _EMPTY_TRACK:
                        empty_count += 1
                    elif result is None:
                        malformed_count += 1
                    else:
                        yield result
                    track_index += 1
                else:
                    trk_lines.append(line)

    # If we ended mid-track (truncated file), try to parse what we have
    if in_trk and trk_lines:
        logger.warning(
            f"File {filepath.name} appears truncated — "
            f"track #{track_index + 1} was not closed"
        )
        # Try closing the tag and parsing
        trk_xml = "".join(trk_lines) + "</trk>"
        result = _parse_trk_block(
            trk_xml, filepath.name, track_index, ns_attrs
        )
        if result is _EMPTY_TRACK:
            empty_count += 1
        elif result is None:
            malformed_count += 1
        else:
            yield result
        track_index += 1

    skipped_parts = []
    if malformed_count > 0:
        skipped_parts.append(f"{malformed_count} malformed")
    if empty_count > 0:
        skipped_parts.append(f"{empty_count} with no GPS data")
    if skipped_parts:
        logger.info(
            f"{filepath.name}: skipped {', '.join(skipped_parts)}"
        )

    imported = track_index - malformed_count - empty_count
    logger.info(
        f"{filepath.name}: extracted {track_index} track blocks, "
        f"{imported} with GPS data"
    )


def parse_gpx_file(filepath: str | Path) -> dict | list[dict]:
    """
    Parse a .GPX file and return GPS trackpoints.

    Only extracts <trk> (recorded tracks), not <rte> (planned routes).

    Uses chunked streaming parsing to handle very large files (multi-GB)
    without loading the entire document into memory. Recovers from
    malformed XML by parsing each track independently.

    If the file contains multiple tracks, returns a list of result dicts
    (one per track).  If it contains a single track, returns a single dict
    for backward compatibility.

    Returns:
        dict or list[dict], each with:
        {
            "points": [(lat, lon), ...],
            "start_time": str (ISO 8601) | None,
            "end_time": str (ISO 8601) | None,
            "filename": str,
            "error": str | None
        }
    """
    filepath = Path(filepath)

    try:
        results = list(parse_gpx_file_streaming(filepath))

        if not results:
            return {
                "points": [],
                "start_time": None,
                "end_time": None,
                "filename": filepath.name,
                "error": None,
            }

        # Assign filenames based on track count
        for i, trk in enumerate(results):
            if len(results) > 1:
                tname = trk.pop("track_name", None) or f"track {i + 1}"
                trk["filename"] = (
                    f"{filepath.stem} [{tname}]{filepath.suffix}"
                )
            else:
                trk.pop("track_name", None)
                trk["filename"] = filepath.name

            logger.info(
                f"Parsed {trk['filename']}: {len(trk['points'])} points"
            )

        if len(results) == 1:
            return results[0]

        return results

    except Exception as e:
        logger.error(f"Failed to parse {filepath.name}: {e}")
        return {
            "points": [],
            "start_time": None,
            "end_time": None,
            "filename": filepath.name,
            "error": str(e),
        }
