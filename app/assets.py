"""Static asset URLs with content hashes, long-lived browser caching and gzip responses.

* ``asset_url("style.css")`` -> ``/static/style.css?v=topnav-<sha256[:12]>``. The hash changes
  whenever the file changes, so a versioned URL can be cached for a year ("immutable") and
  browsers stop re-validating the stylesheet / script on every page (one round trip saved
  per navigation, which is noticeable through the Cloudflare tunnel).
* ``compress_response`` gzips HTML/CSS/JS/JSON when the client accepts it. Static files are
  compressed once and kept in memory (keyed by path, mtime and size).
"""

from __future__ import annotations

import gzip
import hashlib
import os
from threading import Lock

from flask import current_app, request, url_for
from werkzeug.security import safe_join

STATIC_VERSION_PREFIX = "topnav-"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
COMPRESSIBLE_TYPES = frozenset({
    "text/html",
    "text/css",
    "text/plain",
    "text/javascript",
    "application/javascript",
    "application/json",
    "image/svg+xml",
})
MIN_COMPRESS_BYTES = 1024

_lock = Lock()
_versions: dict[tuple, str] = {}
_gzipped: dict[tuple, bytes] = {}


def _static_file(filename: str):
    folder = current_app.static_folder
    path = safe_join(folder, filename) if folder else None
    if not path:
        return None, None
    try:
        stat = os.stat(path)
    except OSError:
        return None, None
    return path, (path, stat.st_mtime_ns, stat.st_size)


def asset_version(filename: str) -> str | None:
    path, key = _static_file(filename)
    if key is None:
        return None
    version = _versions.get(key)
    if version is None:
        with open(path, "rb") as handle:
            version = hashlib.sha256(handle.read()).hexdigest()[:12]
        with _lock:
            _versions[key] = version
    return version


def asset_url(filename: str) -> str:
    version = asset_version(filename)
    if version is None:
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=STATIC_VERSION_PREFIX + version)


def static_cache_headers(response):
    """Versioned static URLs are immutable; unversioned ones keep ETag re-validation."""
    if request.endpoint == "static" and response.status_code in (200, 304) and request.args.get("v"):
        response.headers["Cache-Control"] = IMMUTABLE_CACHE
    return response


def _gzip_static(response) -> bytes | None:
    filename = (request.view_args or {}).get("filename") or ""
    path, key = _static_file(filename)
    if key is None:
        return None
    data = _gzipped.get(key)
    if data is None:
        with open(path, "rb") as handle:
            data = gzip.compress(handle.read(), compresslevel=9, mtime=0)
        with _lock:
            _gzipped[key] = data
    response.close()  # release the file handle opened by send_file
    response.direct_passthrough = False
    return data


def compress_response(response):
    if response.status_code != 200 or response.headers.get("Content-Encoding"):
        return response
    if response.mimetype not in COMPRESSIBLE_TYPES:
        return response
    response.vary.add("Accept-Encoding")
    if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
        return response
    if request.endpoint == "static" and response.direct_passthrough:
        data = _gzip_static(response)
        if data is None:
            return response
    else:
        if response.direct_passthrough or response.is_streamed:
            return response
        raw = response.get_data()
        if len(raw) < MIN_COMPRESS_BYTES:
            return response
        data = gzip.compress(raw, compresslevel=6)
    response.set_data(data)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(data))
    etag = response.headers.get("ETag")
    if etag and not etag.startswith("W/"):
        # The gzip body differs byte-for-byte; a weak tag still matches If-None-Match.
        response.headers["ETag"] = "W/" + etag
    return response
