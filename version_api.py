#!/usr/bin/env python3
from __future__ import annotations

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
from typing import Any, List, Optional, Union
from pydantic import Field

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

VERSION_JSON_PATH = os.environ.get(
    "VERSION_JSON_PATH",
    "/home/github-deploy/nginx/html/version.json",
)
DOWNLOAD_BASE_URL = os.environ.get(
    "VERSION_DOWNLOAD_BASE_URL",
    "http://lightinator.de",
)
# Local filesystem root that nginx serves (siblings of version.json). Used to
# stat webapp bundle files when version.json doesn't carry explicit sizes.
HTML_ROOT = os.environ.get(
    "VERSION_HTML_ROOT",
    os.path.dirname(VERSION_JSON_PATH),
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


class FirmwareEntry(BaseModel):
    version: str = ""
    soc: str = ""
    branch: str = ""
    type: str = ""
    url: str = ""
    comment: str = ""


class WebappFile(BaseModel):
    filename: str = ""
    md5: str = ""
    size: int = Field(default=0, ge=0)


class WebappEntry(BaseModel):
    version: str = ""
    branch: str = ""
    type: str = "debug"
    basepath: str = ""
    min_firmware: str = ""
    total_size: int = Field(default=0, ge=0)
    files: list[WebappFile] = Field(default_factory=list)
    comment: str = ""


def firmware_entry_to_model(entry: dict) -> FirmwareEntry:
    return {
        "version": entry.get("fw_version", entry.get("version", "")),
        "soc": entry.get("soc", ""),
        "branch": entry.get("branch", ""),
        "type": entry.get("type", ""),
        "url": entry.get("files", {}).get("rom", {}).get("url", ""),
        "comment": entry.get("comment", ""),
    }


def webapp_entry_to_model(entry: dict) -> WebappEntry:
    branch = entry.get("branch", "")
    version = entry.get("version", "")
    build_type = entry.get("type", "debug")
    basepath = f"{DOWNLOAD_BASE_URL}/download/webui/{branch}/{version}/"

    def file_size(f: dict) -> int:
        # Prefer an explicit size from version.json; fall back to stat'ing the
        # deployed file on disk so firmware gets sizes even for older entries.
        stored = f.get("size")
        if stored:
            try:
                return int(stored)
            except (TypeError, ValueError):
                pass
        name = f.get("path", "")
        if not name:
            return 0
        local = os.path.join(HTML_ROOT, "download", "webui", branch, version, name)
        try:
            return os.path.getsize(local)
        except OSError:
            return 0

    files_with_names: list[WebappFile] = []
    total_size = 0
    for f in entry.get("files", []):
        size = file_size(f)
        total_size += size
        files_with_names.append(WebappFile(
            filename=f.get("path", ""),
            md5=f.get("md5", ""),
            size=size,
        ))
    return WebappEntry(
        version=version,
        branch=branch,
        type=build_type,
        basepath=basepath,
        min_firmware=entry.get("min_firmware", ""),
        total_size=total_size,
        files=files_with_names,
        comment=entry.get("comment", ""),
    )


app = FastAPI(
    title="Lightinator Version API",
    description="Read-only firmware and webapp catalogue backed by version.json.",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


def version_data() -> dict:
    data = load_version_json()
    if not data:
        raise HTTPException(status_code=503, detail="version.json unavailable")
    return data


def firmware_entries(data: dict, soc: Optional[str], branch: Optional[str], ftype: Optional[str]) -> List[dict]:
    entries = data.get("firmware", []) + data.get("history", [])
    return [e for e in entries if
            (soc is None or e.get("soc", "").lower() == soc.lower()) and
            (branch is None or e.get("branch", "") == branch) and
            (ftype is None or e.get("type", "") == ftype)]


def webapp_entries(data: dict, branch: Optional[str], firmware_version: Optional[str]) -> List[dict]:
    entries = data.get("webapp", []) + data.get("webapp_history", [])
    result = [e for e in entries if branch is None or e.get("branch") == branch]
    if firmware_version:
        result = [e for e in result if not e.get("min_firmware") or
                  version_ge(firmware_version, e["min_firmware"])]
    return result


@app.get("/api/firmware/socs", response_model=list[str], tags=["firmware"])
def firmware_socs(branch: Optional[str] = None, type: Optional[str] = None):
    return sorted({e.get("soc", "") for e in firmware_entries(version_data(), None, branch, type) if e.get("soc")})


@app.get("/api/firmware/branches", response_model=list[str], tags=["firmware"])
def firmware_branches(soc: Optional[str] = None, type: Optional[str] = None):
    return sorted({e.get("branch", "") for e in firmware_entries(version_data(), soc, None, type) if e.get("branch")})


@app.get("/api/firmware/types", response_model=list[str], tags=["firmware"])
def firmware_types(soc: Optional[str] = None, branch: Optional[str] = None):
    return sorted({e.get("type", "") for e in firmware_entries(version_data(), soc, branch, None) if e.get("type")})


@app.get("/api/firmware/versions", response_model=list[FirmwareEntry], tags=["firmware"])
def firmware_versions(soc: Optional[str] = None, branch: Optional[str] = None, type: Optional[str] = None):
    entries = firmware_entries(version_data(), soc, branch, type)
    entries.sort(key=lambda e: parse_version_components(e.get("fw_version", e.get("version", ""))), reverse=True)
    return [firmware_entry_to_model(e) for e in entries]


@app.get("/api/firmware/latest", response_model=Union[FirmwareEntry, List[FirmwareEntry]], tags=["firmware"])
def firmware_latest(soc: Optional[str] = None, branch: Optional[str] = None, type: Optional[str] = None):
    entries = firmware_entries(version_data(), soc, branch, type)
    if not entries:
        raise HTTPException(404, f"no firmware matching soc={soc} branch={branch} type={type}")
    if soc is not None and branch is not None and type is not None:
        return firmware_entry_to_model(max(entries, key=lambda e: parse_version_components(e.get("fw_version", e.get("version", "")))))
    groups: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        key = (entry.get("soc", ""), entry.get("branch", ""), entry.get("type", ""))
        if key not in groups or parse_version_components(entry.get("fw_version", entry.get("version", ""))) > parse_version_components(groups[key].get("fw_version", groups[key].get("version", ""))):
            groups[key] = entry
    return [firmware_entry_to_model(e) for e in sorted(groups.values(), key=lambda e: (e.get("soc", ""), e.get("branch", ""), e.get("type", "")))]


@app.get("/api/webapp/branches", response_model=list[str], tags=["webapp"])
def webapp_branches():
    return sorted({e.get("branch", "") for e in webapp_entries(version_data(), None, None) if e.get("branch")})


@app.get("/api/webapp/versions", response_model=list[WebappEntry], tags=["webapp"])
def webapp_versions(branch: Optional[str] = None, firmware_version: Optional[str] = None):
    entries = webapp_entries(version_data(), branch, firmware_version)
    entries.sort(key=lambda e: parse_version_components(e.get("version", "")), reverse=True)
    return [webapp_entry_to_model(e) for e in entries]


@app.get("/api/webapp/latest", response_model=Union[WebappEntry, List[WebappEntry]], tags=["webapp"])
def webapp_latest(branch: Optional[str] = None, firmware_version: Optional[str] = None):
    entries = webapp_entries(version_data(), branch, firmware_version)
    if not entries:
        raise HTTPException(404, f"no compatible webapp for branch={branch} firmware_version={firmware_version}")
    if branch is not None:
        return webapp_entry_to_model(max(entries, key=lambda e: parse_version_components(e.get("version", ""))))
    groups: dict[str, dict] = {}
    for entry in entries:
        b = entry.get("branch", "")
        if b not in groups or parse_version_components(entry.get("version", "")) > parse_version_components(groups[b].get("version", "")):
            groups[b] = entry
    return [webapp_entry_to_model(e) for e in sorted(groups.values(), key=lambda e: e.get("branch", ""))]
