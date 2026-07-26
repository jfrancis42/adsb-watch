#!/usr/bin/env python3
"""Parse a KML/KMZ overlay into a JSON-serializable dict for the web radar.

Used for static geographic overlays (e.g. the Colorado Pilots Association
practice-area boundaries in COPA_v7_*.kmz): polygon boundaries, lines, and
point labels drawn under the aircraft. Off by default; enabled with --kml.

The result is intentionally flat — every placemark becomes one polygon / line /
point entry with its <name>. We don't preserve the folder hierarchy or styles;
the radar draws everything in one overlay color.

Coordinates are emitted as [lat, lon] pairs to match the web UI's
latLonToXY(lat, lon) signature (KML stores them lon,lat,alt).
"""
import os
import zipfile
import xml.etree.ElementTree as ET

_KML_NS = {'k': 'http://www.opengis.net/kml/2.2'}


def _read_kml_text(path):
    """Return the KML XML text from a .kml file or the doc.kml inside a .kmz."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            # A KMZ's root KML is conventionally doc.kml, but any .kml at the
            # root is acceptable — take doc.kml if present, else the first .kml.
            names = z.namelist()
            kml_name = 'doc.kml' if 'doc.kml' in names else next(
                (n for n in names if n.lower().endswith('.kml')), None)
            if kml_name is None:
                raise ValueError(f'no .kml entry found inside {path}')
            return z.read(kml_name).decode('utf-8')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _parse_coords(text):
    """KML coordinate string 'lon,lat,alt lon,lat,alt ...' -> [[lat, lon], ...]."""
    out = []
    for tok in (text or '').split():
        parts = tok.split(',')
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        out.append([lat, lon])
    return out


def load(path):
    """Parse a KML/KMZ file into an overlay dict.

    Returns:
        {
          'name': <document name or basename>,
          'polygons': [{'name': str, 'coords': [[lat, lon], ...]}, ...],
          'lines':    [{'name': str, 'coords': [[lat, lon], ...]}, ...],
          'points':   [{'name': str, 'lat': float, 'lon': float}, ...],
        }
    Raises OSError if the file can't be read, ValueError/ET.ParseError if it
    isn't parseable KML.
    """
    xml = _read_kml_text(path)
    root = ET.fromstring(xml)

    doc = root.find('.//k:Document', _KML_NS)
    doc_name = None
    if doc is not None:
        n = doc.find('k:name', _KML_NS)
        if n is not None and n.text:
            doc_name = n.text.strip()

    overlay = {
        'name': doc_name or os.path.basename(path),
        'polygons': [],
        'lines': [],
        'points': [],
    }

    for pm in root.findall('.//k:Placemark', _KML_NS):
        name_el = pm.find('k:name', _KML_NS)
        name = name_el.text.strip() if (name_el is not None and name_el.text) else ''

        # A placemark carries exactly one of these in this dataset; handle each
        # geometry the placemark actually contains.
        for poly in pm.findall('.//k:Polygon', _KML_NS):
            # Only the outer boundary — inner holes aren't meaningful for a
            # practice-area outline and the radar draws outlines, not fills.
            coords_el = poly.find('.//k:outerBoundaryIs//k:coordinates', _KML_NS)
            if coords_el is None:
                coords_el = poly.find('.//k:coordinates', _KML_NS)
            if coords_el is not None:
                coords = _parse_coords(coords_el.text)
                if coords:
                    overlay['polygons'].append({'name': name, 'coords': coords})

        for line in pm.findall('.//k:LineString', _KML_NS):
            coords_el = line.find('.//k:coordinates', _KML_NS)
            if coords_el is not None:
                coords = _parse_coords(coords_el.text)
                if coords:
                    overlay['lines'].append({'name': name, 'coords': coords})

        for pt in pm.findall('.//k:Point', _KML_NS):
            coords_el = pt.find('.//k:coordinates', _KML_NS)
            if coords_el is not None:
                coords = _parse_coords(coords_el.text)
                if coords:
                    lat, lon = coords[0]
                    overlay['points'].append({'name': name, 'lat': lat, 'lon': lon})

    return overlay


if __name__ == '__main__':
    import sys
    import json
    if len(sys.argv) != 2:
        print('usage: kml.py <file.kml|file.kmz>')
        raise SystemExit(2)
    ov = load(sys.argv[1])
    print(f"name:     {ov['name']}")
    print(f"polygons: {len(ov['polygons'])}")
    print(f"lines:    {len(ov['lines'])}")
    print(f"points:   {len(ov['points'])}")
    if '-v' in sys.argv or True:
        print(json.dumps(ov)[:500])
