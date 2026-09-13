"""Local dev server for the map.

`python -m http.server` sends no cache headers at all, which leaves browsers to
cache heuristically -- so an edited app.js or a rebuilt data file keeps serving
the stale copy and the map appears not to have changed. That is a genuinely
confusing failure, because the pipeline output on disk is correct and only the
browser disagrees.

This is the same static server with caching turned off, and with gzip for the
big JSON payloads (the boundary file is 21 MB raw, 3.4 MB compressed).

    python serve.py [port]
"""

from __future__ import annotations

import functools
import gzip
import io
import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
COMPRESSIBLE = {".json", ".geojson", ".js", ".css", ".html", ".svg"}
MIN_GZIP_BYTES = 4096

# Local .js/.css references in index.html, skipping absolute CDN URLs.
LOCAL_ASSET = re.compile(r'(src|href)="(?!https?:|//)([^"?]+\.(?:js|css))"')


class DevHandler(SimpleHTTPRequestHandler):
    def _versioned_index(self, path: Path) -> bytes:
        """Stamp local script and stylesheet URLs with their file mtime.

        `no-store` alone is not enough in practice: some browsers -- and the
        embedded pane this was developed against -- keep serving a cached
        `app.js` even when the server forbids storing it, so an edit appears to
        do nothing while the file on disk is plainly correct. A changing query
        string makes the URL itself new, which nothing can cache past.
        """
        html = path.read_text(encoding="utf-8")

        def stamp(match):
            attr, rel = match.group(1), match.group(2)
            target = WEB_DIR / rel
            version = int(target.stat().st_mtime) if target.exists() else 0
            return attr + '="' + rel + "?v=" + str(version) + '"'

        return LOCAL_ASSET.sub(stamp, html).encode("utf-8")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")

        if path.name == "index.html" and path.is_file():
            payload = self._versioned_index(path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return io.BytesIO(payload)

        if (not accepts_gzip or not path.is_file()
                or path.suffix.lower() not in COMPRESSIBLE
                or path.stat().st_size < MIN_GZIP_BYTES):
            return super().send_head()

        payload = gzip.compress(path.read_bytes(), 6)
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        return io.BytesIO(payload)

    def log_message(self, fmt, *args):
        # Quiet: one line per tile request is noise when the map loads 43k areas.
        if "404" in (fmt % args):
            super().log_message(fmt, *args)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    handler = functools.partial(DevHandler, directory=str(WEB_DIR))
    print(f"serving {WEB_DIR} at http://localhost:{port}  (caching disabled)")
    HTTPServer(("127.0.0.1", port), handler).serve_forever()


if __name__ == "__main__":
    main()
