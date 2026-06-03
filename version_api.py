#!/usr/bin/env python3
"""
lightinator.de Version API
Read-only HTTP API over version.json for firmware and webapp version queries.

Listens on 0.0.0.0:8765 (all interfaces on the host). nginx runs in a Podman
container and cannot reach the host via 127.0.0.1; it connects via
host.containers.internal:8765 instead. Port 8765 is not opened in the
firewall, so it is only reachable from the container network.
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
LISTEN_HOST = os.environ.get("VERSION_API_HOST", "0.0.0.0")
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
    #
    # All filter axes are optional query parameters:
    #   ?soc=<soc>  &branch=<branch>  &type=<type>
    #
    # Endpoints:
    #   GET /api/firmware/socs
    #   GET /api/firmware/branches[?soc=][&type=]
    #   GET /api/firmware/types[?soc=][&branch=]
    #   GET /api/firmware/versions[?soc=][&branch=][&type=]
    #   GET /api/firmware/latest[?soc=][&branch=][&type=]
    # ------------------------------------------------------------------

    def _handle_firmware(self, parts, qs, data):
        all_entries = data.get("firmware", []) + data.get("history", [])

        soc    = (qs.get("soc")    or [None])[0]
        branch = (qs.get("branch") or [None])[0]
        ftype  = (qs.get("type")   or [None])[0]

        def fw_ver(e):
            return e.get("fw_version", e.get("version", ""))

        def apply_filters(entries):
            return [
                e for e in entries
                if (soc    is None or e.get("soc",    "").lower() == soc.lower())
                and (branch is None or e.get("branch", "")         == branch)
                and (ftype  is None or e.get("type",   "")         == ftype)
            ]

        if not parts:
            not_found(self)
            return

        resource = parts[0]

        # /firmware/socs — list distinct SOC values (filters: branch, type)
        if resource == "socs":
            entries = apply_filters(all_entries)
            socs = sorted({e.get("soc", "") for e in entries if e.get("soc")})
            json_response(self, 200, socs)

        # /firmware/branches — list distinct branch values (filters: soc, type)
        elif resource == "branches":
            entries = apply_filters(all_entries)
            branches = sorted({e.get("branch", "") for e in entries if e.get("branch")})
            json_response(self, 200, branches)

        # /firmware/types — list distinct type values (filters: soc, branch)
        elif resource == "types":
            entries = apply_filters(all_entries)
            types = sorted({e.get("type", "") for e in entries if e.get("type")})
            json_response(self, 200, types)

        # /firmware/versions — all matching entries, newest first
        elif resource == "versions":
            entries = apply_filters(all_entries)
            entries.sort(key=lambda e: parse_version_components(fw_ver(e)), reverse=True)
            json_response(self, 200, [firmware_entry_to_dict(e) for e in entries])

        # /firmware/latest — newest entry per unique (soc, branch, type) slice
        elif resource == "latest":
            entries = apply_filters(all_entries)
            if not entries:
                not_found(self, f"no firmware matching soc={soc} branch={branch} type={ftype}")
                return
            # When all three axes are fixed, return a single object.
            # Otherwise return one latest entry per unique remaining combination.
            if soc is not None and branch is not None and ftype is not None:
                best = max(entries, key=lambda e: parse_version_components(fw_ver(e)))
                json_response(self, 200, firmware_entry_to_dict(best))
            else:
                groups: dict = {}
                for e in entries:
                    key = (e.get("soc", ""), e.get("branch", ""), e.get("type", ""))
                    if key not in groups or (
                        parse_version_components(fw_ver(e))
                        > parse_version_components(fw_ver(groups[key]))
                    ):
                        groups[key] = e
                result = sorted(groups.values(), key=lambda e: (e.get("soc", ""), e.get("branch", ""), e.get("type", "")))
                json_response(self, 200, [firmware_entry_to_dict(e) for e in result])

        else:
            not_found(self)

    # ------------------------------------------------------------------
    # Webapp routes
    #
    # Filter axes are optional query parameters:
    #   ?branch=<branch>  &firmware_version=<ver>
    #
    # Endpoints:
    #   GET /api/webapp/branches
    #   GET /api/webapp/versions[?branch=][&firmware_version=]
    #   GET /api/webapp/latest[?branch=][&firmware_version=]
    # ------------------------------------------------------------------

    def _handle_webapp(self, parts, qs, data):
        all_entries = data.get("webapp", []) + data.get("webapp_history", [])

        branch           = (qs.get("branch")           or [None])[0]
        firmware_version = (qs.get("firmware_version") or [None])[0]

        def apply_filters(entries):
            filtered = [e for e in entries if branch is None or e.get("branch") == branch]
            if firmware_version:
                filtered = [
                    e for e in filtered
                    if not e.get("min_firmware") or version_ge(firmware_version, e["min_firmware"])
                ]
            return filtered

        if not parts:
            not_found(self)
            return

        resource = parts[0]

        # /webapp/branches
        if resource == "branches":
            branches = sorted({e.get("branch", "") for e in all_entries if e.get("branch")})
            json_response(self, 200, branches)

        # /webapp/versions — all matching entries, newest first
        elif resource == "versions":
            entries = apply_filters(all_entries)
            entries.sort(key=lambda e: parse_version_components(e.get("version", "")), reverse=True)
            json_response(self, 200, [webapp_entry_to_dict(e) for e in entries])

        # /webapp/latest — newest per branch when branch unspecified; single when specified
        elif resource == "latest":
            entries = apply_filters(all_entries)
            if not entries:
                not_found(self, f"no compatible webapp for branch={branch} firmware_version={firmware_version}")
                return
            if branch is not None:
                best = max(entries, key=lambda e: parse_version_components(e.get("version", "")))
                json_response(self, 200, webapp_entry_to_dict(best))
            else:
                groups: dict = {}
                for e in entries:
                    b = e.get("branch", "")
                    if b not in groups or (
                        parse_version_components(e.get("version", ""))
                        > parse_version_components(groups[b].get("version", ""))
                    ):
                        groups[b] = e
                result = sorted(groups.values(), key=lambda e: e.get("branch", ""))
                json_response(self, 200, [webapp_entry_to_dict(e) for e in result])

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
