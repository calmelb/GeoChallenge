#!/usr/bin/env python3
"""
Fetch a Google Street View panorama and save it to the repo.

Usage:
    python update_panorama.py "<google_maps_url>"

Saves:
    cur_geo.jpg              — overwrites the current panorama
    geochallenge_N.jpg       — archived copy (N = next increment)

Then commits and pushes both files.

Requirements:
    pip install requests Pillow
"""

import re
import sys
import json
import math
import subprocess
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image


REPO_DIR = Path(__file__).parent
TILE_ZOOM = 3          # 8×4 = 32 tiles → ~4096×2048 equirectangular
JPEG_QUALITY = 92


# ── URL parsing ───────────────────────────────────────────────────────────────

def parse_maps_url(raw: str) -> dict:
    """Extract pano ID and/or lat/lng from a Google Maps Street View URL."""
    result = {"pano_id": None, "lat": None, "lng": None}

    at_match = re.search(r"@(-?\d+\.?\d*),(-?\d+\.?\d*)", raw)
    if at_match:
        result["lat"] = float(at_match.group(1))
        result["lng"] = float(at_match.group(2))

    pano_match = re.search(r"!1s([A-Za-z0-9_\-]{10,})", raw)
    if pano_match:
        result["pano_id"] = pano_match.group(1)

    return result


# ── Pano ID lookup from coordinates (unofficial) ──────────────────────────────

def get_pano_id(lat: float, lng: float) -> str:
    pb = (
        f"!1m5!1sapiv3!5sUS!11m2!1m1!1b0"
        f"!2m4!1m2!3d{lat}!4d{lng}!2d50"
        f"!3m10!2m2!1sen!2sUS!9m1!1e2"
        f"!11m4!1m3!1e2!2b1!3e2"
        f"!4m10!1e1!1e2!1e3!1e4!1e8!1e6"
    )
    url = f"https://maps.googleapis.com/maps/api/js/GeoPhotoService.SingleImageSearch?pb={pb}&callback=cb"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()

    # Response is JSONP: cb([[...], [...]])  — strip the wrapper
    text = resp.text.strip()
    json_str = re.sub(r"^cb\(", "", text).rstrip(");")
    data = json.loads(json_str)

    try:
        pano_id = (
            data[1][0][0][0][1]
            if data[1] and data[1][0] and data[1][0][0] and data[1][0][0][0]
            else data[0][0][0][0]
        )
        if not pano_id or not isinstance(pano_id, str):
            raise ValueError()
        return pano_id
    except (IndexError, ValueError, TypeError):
        raise RuntimeError("No Street View coverage found at this location")


# ── Tile fetching & stitching ─────────────────────────────────────────────────

def tile_url(pano_id: str, zoom: int, x: int, y: int) -> str:
    return (
        f"https://streetviewpixels-pa.googleapis.com/v1/tile"
        f"?cb_client=maps_sv.tactile&panoid={pano_id}&x={x}&y={y}&zoom={zoom}"
    )


def fetch_panorama(pano_id: str) -> Image.Image:
    cols = int(math.pow(2, TILE_ZOOM))      # 8
    rows = int(math.pow(2, TILE_ZOOM - 1))  # 4
    total = cols * rows

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Fetch first tile to determine tile dimensions
    resp = session.get(tile_url(pano_id, TILE_ZOOM, 0, 0), timeout=15)
    resp.raise_for_status()
    first_tile = Image.open(BytesIO(resp.content))
    tile_w, tile_h = first_tile.size

    canvas = Image.new("RGB", (cols * tile_w, rows * tile_h))
    canvas.paste(first_tile, (0, 0))

    fetched = 1
    print(f"  [{fetched:2d}/{total}] tile 0,0")

    for y in range(rows):
        for x in range(cols):
            if x == 0 and y == 0:
                continue
            resp = session.get(tile_url(pano_id, TILE_ZOOM, x, y), timeout=15)
            resp.raise_for_status()
            tile = Image.open(BytesIO(resp.content))
            canvas.paste(tile, (x * tile_w, y * tile_h))
            fetched += 1
            print(f"  [{fetched:2d}/{total}] tile {x},{y}")

    return canvas


# ── File naming ───────────────────────────────────────────────────────────────

def next_filename() -> str:
    max_n = 0
    for p in REPO_DIR.glob("geochallenge_*.jpg"):
        m = re.match(r"geochallenge_(\d+)\.jpg$", p.name, re.IGNORECASE)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"geochallenge_{max_n + 1}.jpg"


# ── Git helpers ───────────────────────────────────────────────────────────────

def git(*args):
    result = subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python update_panorama.py \"<google_maps_url>\"")
        sys.exit(1)

    raw_url = sys.argv[1]

    print("Parsing URL…")
    parsed = parse_maps_url(raw_url)

    pano_id = parsed["pano_id"]
    if not pano_id:
        if parsed["lat"] is None:
            print("Error: could not extract pano ID or coordinates from URL")
            sys.exit(1)
        print(f"No pano ID in URL — looking up coverage at {parsed['lat']}, {parsed['lng']}…")
        pano_id = get_pano_id(parsed["lat"], parsed["lng"])

    print(f"Pano ID: {pano_id}")

    print(f"\nFetching tiles (zoom={TILE_ZOOM}, {2**TILE_ZOOM}×{2**(TILE_ZOOM-1)} grid)…")
    image = fetch_panorama(pano_id)
    print(f"Stitched: {image.width}×{image.height}px")

    archive_name = next_filename()
    cur_path     = REPO_DIR / "cur_geo.jpg"
    archive_path = REPO_DIR / archive_name

    print(f"\nSaving {archive_name}…")
    image.save(archive_path, "JPEG", quality=JPEG_QUALITY)

    print("Saving cur_geo.jpg…")
    image.save(cur_path, "JPEG", quality=JPEG_QUALITY)

    print("\nCommitting…")
    git("add", "cur_geo.jpg", archive_name)
    git("commit", "-m", f"Update panorama → {archive_name}")

    print("Pushing…")
    git("push")

    print(f"\nDone. Saved as {archive_name} and cur_geo.jpg, pushed to remote.")


if __name__ == "__main__":
    main()
