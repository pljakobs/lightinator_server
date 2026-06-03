#!/usr/bin/env python3
"""
lightinator.de Version API
Read-only HTTP API over version.json for firmware and webapp version queries.
Listens on 127.0.0.1:8765; nginx proxies /api/ to it.
"""

import json
import os
import time
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERSION_JSON_PATH = os.environ.get(
    "VERSION_JSON_PATH",
    "/home/github-deploy/nginx/html/version.json",
)
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("VERSION_API_PORT", "8765"))
CACHE_TTL = 10  # seconds


# ---------------------------------------------------------------------------
# Version comparison (mirrors parse_version_components in manage_version.py)
# ---------------------------------------------------------------------------

def parse_version_components(version_string: str) -> tuple:
    """Parse firmware version string into a sortable 4-tuple."""
    normalized = version_string.strip().lstrip("Vv")
    if not normalized:
        return (0, 0, 0, 0)
    segments = normalized.split("-")
    base_parts = segments[0].split(".")
    try:
        major = int(base_parts[0]) if len(base_parts) > 0 else 0
        minor = int(base_parts[1]) if len(base_parts) > 1 else 0
        patch = int(base_parts[2]) if len(base_parts) > 2 else 0
    except ValueError:
        return (0, 0, 0, 0)
    prerelease = 0
    if len(segments) > 1 and segments[1].isdigit():
        prerelease = int(segments[1])
    return (major, minor, patch, prerelease)


def version_ge(a: str, b: str) -> bool:
    """Return True if version a >= version b."""
    return parse_version_components(a) >= parse_version_components(b)


# ---------------------------------------------------------------------------
# version.json cache
# ---------------------------------------------------------------------------

_cache: dict = {"data": None, "mtime": 0.0, "loaded_at": 0.0}


def load_version_json() -> dict:
    now = time.monotonic()
    try:
        mtime = os.path.getmtime(VERSION_JSON_PATH)
    except OSError:
        return {}

    if (
        _cache["data"] is not None
        and mtime == _cache["mtime"]
        and (now - _cache["loaded_at"]) < CACHE_TTL
    ):
        return _cache["data"]

    try:
        with open(VERSION_JSON_PATH) as f:
            data = json.load(f)
        _cache["data"] = data
        _cache["mtime"] = mtime
        _cache["loaded_at"] = now
        return data
    except (OSError, json.JSONDecodeError):
        return _cache["data"] or {}


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------

def json_response(handler, status: int, body) -> None:
    payload = json.dumps(body, indent=2).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


def not_found(handler, msg="not found") -> None:
    json_response(handler, 404, {"error": msg})


def firmware_entry_to_dict(entry: dict) -> dict:
    return {
        "version": entry.get("fw_version", entry.get("version", "")),
        "soc": entry.get("soc", ""),
        "branch": entry.get("branch", ""),
        "type": entry.get("type", ""),
        "url": entry.get("files", {}).get("rom", {}).get("url", ""),
        "comment": entry.get("comment", ""),
    }


def webapp_entry_to_dict(entry: dict) -> dict:
    return {
        "version": entry.get("version", ""),
        "branch": entry.get("branch", ""),
        "type": entry.get("type", "debug"),
        "min_firmware": entry.get("min_firmware", ""),
        "files": entry.get("files", []),
        "comment": entry.get("comment", ""),
    }


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class VersionAPIHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # suppress default access log noise
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        # Strip leading /api/ prefix that nginx may or may not strip
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]  # leave leading /
        qs = parse_qs(parsed.query)

        # Split into segments, drop empty strings
        parts = [p for p in path.split("/") if p]

        data = load_version_json()
        if not data:
            json_response(self, 503, {"error": "version.json unavailable"})
            return

        # Route: firmware
        if parts and parts[0] == "firmware":
            self._handle_firmware(parts[1:], qs, data)
        elif parts and parts[0] == "webapp":
            self._handle_webapp(parts[1:], qs, data)
        else:
            not_found(self)

    # ------------------------------------------------------------------
    # Firmware routes
    # ------------------------------------------------------------------

    def _handle_firmware(self, parts, qs, data):
        firmware = data.get("firmware", [])

        # /firmware/<soc>/branches
        if len(parts) == 2 and parts[1] == "branches":
            soc = parts[0]
            branches = sorted({e["branch"] for e in firmware if e.get("soc", "").lower() == soc})
            json_response(self, 200, branches)

        # /firmware/<soc>/versions[/<branch>[/<type>]]
        elif len(parts) >= 2 and parts[1] == "versions":
            soc = parts[0]
            branch_filter = parts[2] if len(parts) > 2 else None
            type_filter = parts[3] if len(parts) > 3 else None

            entries = [
                e for e in firmware
                if e.get("soc", "").lower() == soc
                and (branch_filter is None or e.get("branch") == branch_filter)
                and (type_filter is None or e.get("type") == type_filter)
            ]
            # Also include history entries
            history = data.get("history", [])
            entries += [
                e for e in history
                if e.get("soc", "").lower() == soc
                and (branch_filter is None or e.get("branch") == branch_filter)
                and (type_filter is None or e.get("type") == type_filter)
            ]
            entries.sort(
                key=lambda e: parse_version_components(e.get("fw_version", e.get("version", ""))),
                reverse=True,
            )
            json_response(self, 200, [firmware_entry_to_dict(e) for e in entries])

        # /firmware/<soc>/latest/<branch>/<type>
        elif len(parts) == 4 and parts[1] == "latest":
            soc, _, branch, fw_type = parts
            candidates = [
                e for e in firmware
                if e.get("soc", "").lower() == soc
                and e.get("branch") == branch
                and e.get("type") == fw_type
            ]
            if not candidates:
                not_found(self, f"no {soc}/{branch}/{fw_type} firmware entry")
                return
            best = max(
                candidates,
                key=lambda e: parse_version_components(e.get("fw_version", e.get("version", ""))),
            )
            json_response(self, 200, firmware_entry_to_dict(best))

        else:
            not_found(self)

    # ------------------------------------------------------------------
    # Webapp routes
    # ------------------------------------------------------------------

    def _handle_webapp(self, parts, qs, data):
        webapp = data.get("webapp", [])
        firmware_version = (qs.get("firmware_version") or [None])[0]

        # /webapp/branches
        if len(parts) == 1 and parts[0] == "branches":
            branches = sorted({e["branch"] for e in webapp})
            json_response(self, 200, branches)

        # /webapp/versions/<branch>
        elif len(parts) == 2 and parts[0] == "versions":
            branch = parts[1]
            entries = [e for e in webapp if e.get("branch") == branch]
            # Include webapp_history for the same branch
            history = data.get("webapp_history", [])
            entries += [e for e in history if e.get("branch") == branch]
            if firmware_version:
                entries = [
                    e for e in entries
                    if not e.get("min_firmware") or version_ge(firmware_version, e["min_firmware"])
                ]
            entries.sort(
                key=lambda e: parse_version_components(e.get("version", "")),
                reverse=True,
            )
            json_response(self, 200, [webapp_entry_to_dict(e) for e in entries])

        # /webapp/latest/<branch>
        elif len(parts) == 2 and parts[0] == "latest":
            branch = parts[1]
            entries = [e for e in webapp if e.get("branch") == branch]
            if firmware_version:
                entries = [
                    e for e in entries
                    if not e.get("min_firmware") or version_ge(firmware_version, e["min_firmware"])
                ]
            if not entries:
                not_found(self, f"no compatible webapp for branch={branch} firmware_version={firmware_version}")
                return
            best = max(entries, key=lambda e: parse_version_components(e.get("version", "")))
            json_response(self, 200, webapp_entry_to_dict(best))

        else:
            not_found(self)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), VersionAPIHandler)
    print(f"Version API listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"version.json: {VERSION_JSON_PATH}")
    server.serve_forever()
