"""Fetching helpers with on-disk caching.

Downloads are cached in data/raw and never re-fetched unless the file is
missing, so re-running the pipeline is cheap and works offline.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

from config import RAW

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "uk-affluence-map/0.1 (open data pipeline)"})


def fetch_file(url: str, filename: str, *, force: bool = False) -> Path:
    """Download `url` to data/raw/`filename`, returning the cached path."""
    dest = RAW / filename
    if dest.exists() and not force:
        print(f"  cached: {filename} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    print(f"  downloading: {url}")
    with SESSION.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        written = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                written += len(chunk)
                if total:
                    pct = 100 * written / total
                    print(f"\r    {written / 1e6:6.1f} / {total / 1e6:.1f} MB ({pct:5.1f}%)",
                          end="", flush=True)
        print()
        tmp.replace(dest)
    return dest


def fetch_arcgis_geojson(service_url: str, filename: str, fields: list[str],
                         *, page_size: int = 2000, force: bool = False) -> Path:
    """Page through an ArcGIS FeatureServer layer and cache it as GeoJSON.

    ArcGIS caps each response at maxRecordCount (2000 for the ONS services),
    so the full layer has to be assembled from offset queries.
    """
    dest = RAW / filename
    if dest.exists() and not force:
        print(f"  cached: {filename} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    features: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": ",".join(fields),
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        resp = SESSION.get(f"{service_url}/query", params=params, timeout=180)
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("features", [])
        if not batch:
            break
        features.extend(batch)
        print(f"\r    {len(features):,} features", end="", flush=True)
        offset += page_size
        if not payload.get("properties", {}).get("exceededTransferLimit") and len(batch) < page_size:
            break
        time.sleep(0.2)  # be polite to the ONS service
    print()

    dest.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return dest
