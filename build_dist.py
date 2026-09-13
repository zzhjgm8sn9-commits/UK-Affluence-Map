"""Assemble a deployable copy of the site in dist/.

`web/` is already a static site, so this mostly copies it. The one thing it must
do is what serve.py does on the fly and a static host cannot: stamp the local
script and stylesheet URLs with a content hash, so that when the site is
redeployed people who have visited before get the new version rather than a
cached one. Without it a returning visitor can sit on a stale app.js
indefinitely and see none of the changes.

    python build_dist.py

Then deploy dist/ to any static host. There is no build step beyond this and no
server-side anything -- the whole thing is files.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DIST = ROOT / "dist"

# The geometry files are .json rather than .geojson deliberately: GitHub Pages
# gzips on the fly by content type, and application/geo+json is not reliably on
# that list. As .geojson the 34.9 MB road file could be served uncompressed.
LOCAL_ASSET = re.compile(r'(src|href)="(?!https?:|//)([^"?]+\.(?:js|css))"')

REQUIRED = [
    "index.html", "app.js", "branches.js", "roads.js", "search.js", "style.css",
    "data/gb_areas.json", "data/gb_metrics.json", "data/gb_income.json",
    "data/gb_branches.json", "data/gb_places.json", "data/gb_search.json",
    "data/gb_roads.json",
]


def _drop_readonly(func, path, exc):
    """Git marks its object files read-only, and Windows refuses to delete
    those. deploy_gh_pages.py leaves a .git directory inside dist/, so a plain
    rmtree fails on every rebuild after the first deploy."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def check_inputs() -> bool:
    missing = [p for p in REQUIRED if not (WEB / p).exists()]
    if missing:
        print("missing generated files -- run the pipeline first:")
        for p in missing:
            print(f"  {p}")
        return False
    if not (WEB / "fonts").exists():
        print("missing web/fonts -- run pipeline/build_places.py")
        return False
    if not (WEB / "data" / "postcodes").exists():
        print("missing web/data/postcodes -- run pipeline/build_search.py")
        return False
    return True


def stamp_index(dist: Path) -> None:
    """Version local assets by content hash, so redeploys bust caches."""
    index = dist / "index.html"
    html = index.read_text(encoding="utf-8")

    def stamp(match):
        attr, rel = match.group(1), match.group(2)
        target = dist / rel
        if not target.exists():
            return match.group(0)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:8]
        return attr + '="' + rel + "?v=" + digest + '"'

    index.write_text(LOCAL_ASSET.sub(stamp, html), encoding="utf-8")


def main() -> None:
    if not check_inputs():
        sys.exit(1)

    if DIST.exists():
        shutil.rmtree(DIST, onexc=_drop_readonly)
    shutil.copytree(WEB, DIST)

    # GitHub Pages runs Jekyll by default, which skips files and folders
    # beginning with an underscore and adds nothing we want.
    (DIST / ".nojekyll").write_text("")

    stamp_index(DIST)

    eager = [p for p in REQUIRED if p.endswith((".json", ".geojson", ".js", ".css", ".html"))]
    eager_gz = sum(len(gzip.compress((DIST / p).read_bytes(), 6)) for p in eager)
    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    files = sum(1 for f in DIST.rglob("*") if f.is_file())

    print(f"dist/ ready: {files:,} files, {total / 1e6:.0f} MB on disk")
    print(f"  first visit downloads about {eager_gz / 1e6:.1f} MB gzipped")
    print(f"  postcode chunks load only when someone searches a postcode")
    print()
    print("deploy dist/ to any static host. Two easy routes:")
    print("  Netlify   drag dist/ onto https://app.netlify.com/drop")
    print("  GH Pages  push dist/ as a gh-pages branch, then enable Pages")


if __name__ == "__main__":
    main()
