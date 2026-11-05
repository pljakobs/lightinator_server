#!/usr/bin/env python3
"""
Lightinator Release & Distribution Service
Provides secure upload endpoints for CI deployment and read-only version discovery for clients.
"""

from __future__ import annotations

import os
import hashlib
import json
import io
import re
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Union

from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration & Environment
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("VERSION_DB_PATH", "firmware.db")
STATIC_ROOT = Path(os.getenv("VERSION_HTML_ROOT", "/var/www/downloads"))
DOWNLOAD_BASE_URL = os.getenv("VERSION_DOWNLOAD_BASE_URL", "http://lightinator.de").rstrip("/")
CI_TOKEN = os.getenv("CI_UPLOAD_TOKEN", "")
PUBLIC_KEY_HEX = os.getenv("RELEASE_PUBLIC_KEY_HEX", "")

LISTEN_HOST = os.getenv("VERSION_API_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("VERSION_API_PORT", "8765"))
OPENAPI_URL = "/api/openapi.json"
DISTRIBUTION_ICON_URL = "/api/brand/lightinator-distribution-server-icon-dark.svg"


# ---------------------------------------------------------------------------
# Database Helper & Initialization
# ---------------------------------------------------------------------------

@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS firmware_builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                soc TEXT NOT NULL,
                type TEXT NOT NULL,
                branch TEXT NOT NULL,
                version TEXT NOT NULL,
                file_path TEXT NOT NULL,
                url TEXT NOT NULL,
                comment TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(soc, branch, type, version)
            );
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS webapp_builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT NOT NULL,
                version TEXT NOT NULL,
                type TEXT DEFAULT 'debug',
                min_firmware TEXT,
                file_path TEXT NOT NULL,
                basepath TEXT NOT NULL,
                comment TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(branch, version)
            );
            """
        )
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(firmware_builds)")}
        for name, kind in {"release_line": "TEXT", "stage": "TEXT", "sha256": "TEXT", "size": "INTEGER",
                           "source_sha": "TEXT", "rgbwwled_version": "TEXT", "upgrade_from_lines": "TEXT"}.items():
            if name not in columns:
                cursor.execute(f"ALTER TABLE firmware_builds ADD COLUMN {name} {kind}")
        webapp_columns = {row[1] for row in cursor.execute("PRAGMA table_info(webapp_builds)")}
        for name in ("max_firmware", "api_min", "api_max", "required_capabilities", "sha256"):
            if name not in webapp_columns:
                cursor.execute(f"ALTER TABLE webapp_builds ADD COLUMN {name} TEXT")
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Lightinator Firmware & Webapp Distribution API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    openapi_url=OPENAPI_URL,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.mount(
        "/api/brand",
        StaticFiles(directory=Path(__file__).parent / "images"),
        name="brand",
)


@app.get("/api/docs", include_in_schema=False)
def distribution_api_docs() -> HTMLResponse:
        response = get_swagger_ui_html(
                openapi_url=OPENAPI_URL,
                title=f"{app.title} - API Docs",
                swagger_favicon_url=DISTRIBUTION_ICON_URL,
        )
        html = response.body.decode("utf-8")
        html = html.replace(
                "</head>",
                """<style>
                    #lightinator-docs-brand {
                        align-items: center;
                        background: #20242b;
                        border-bottom: 1px solid #414850;
                        color: #f3f7f6;
                        display: flex;
                        font: 600 16px system-ui, sans-serif;
                        gap: 12px;
                        padding: 10px 24px;
                    }
                    #lightinator-docs-brand img {
                        display: block;
                        height: 48px;
                        width: 48px;
                    }
                </style></head>""",
                1,
        )
        html = html.replace(
                "<body>",
                f'<body><header id="lightinator-docs-brand">'
                f'<img src="{DISTRIBUTION_ICON_URL}" alt="" width="48" height="48">'
                "<span>Lightinator Distribution Server</span></header>",
                1,
        )
        return HTMLResponse(content=html, status_code=response.status_code)


# ---------------------------------------------------------------------------
# Security & Authentication
# ---------------------------------------------------------------------------

def verify_token(authorization: str = Header(...)) -> None:
    token = authorization.replace("Bearer ", "").strip()
    if not CI_TOKEN or token != CI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing authentication token")


def verify_sig(data: bytes, signature_hex: str) -> None:
    if not PUBLIC_KEY_HEX:
        raise HTTPException(status_code=500, detail="Server public key is unconfigured")
    try:
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX))
        public_key.verify(bytes.fromhex(signature_hex), data)
    except Exception:
        raise HTTPException(status_code=400, detail="Cryptographic signature verification failed")


# ---------------------------------------------------------------------------
# Version Parsing Helper
# ---------------------------------------------------------------------------

def parse_version_components(version_string: str) -> tuple[int, int, int, int]:
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


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class FirmwareEntry(BaseModel):
    version: str
    soc: str
    branch: str
    type: str
    url: str
    comment: str = ""


class WebappEntry(BaseModel):
    version: str
    branch: str
    type: str = "debug"
    basepath: str
    min_firmware: str = ""
    comment: str = ""


def release_identity(version: str, branch: str) -> dict[str, str] | None:
    match = re.match(r"^[Vv]?(\d+)\.(\d+)(?:\.|-|$)", version)
    if not match:
        return None
    line = f"{int(match[1])}.{int(match[2])}"
    normalized = branch.lower().strip()
    qualified = re.fullmatch(r"(\d+\.\d+)[/-](develop|testing|test|prod|stable|experimental)", normalized)
    if qualified:
        if qualified[1] != line:
            return None
        normalized = qualified[2]
    stages = {"develop": "develop", "experimental": "develop", "test": "testing", "testing": "testing", "stable": "prod", "prod": "prod"}
    stage = stages.get(normalized)
    if stage is None:
        return None
    return {"release_line": line, "stage": stage, "source_ref": f"{line}/{stage}"}


def version_sort_key(version: str) -> tuple:
    match = re.fullmatch(r"[Vv]?(\d+)\.(\d+)(?:\.(\d+))?(?:-([^+]+))?(?:\+.*)?", version)
    if not match:
        return (-1, -1, -1, 0, ())
    prerelease = match[4]
    identifiers = prerelease.split(".") if prerelease else []
    legacy = re.match(r"^(\d+)(?:-|$)", prerelease or "")
    if legacy:
        identifiers = [legacy[1]]
    return (int(match[1]), int(match[2]), int(match[3] or 0), int(not prerelease),
            tuple((0, int(value)) if value.isdigit() else (1, value) for value in identifiers))


def firmware_candidates(soc: str) -> list[dict]:
    result = []
    with get_db_connection() as connection:
        metadata = {row["id"]: dict(row) for row in connection.execute("SELECT id, soc, type, branch, version, release_line, stage, sha256, size, source_sha, rgbwwled_version, upgrade_from_lines FROM firmware_builds WHERE soc = ?", (soc.lower(),))}
    indexed = {(row["soc"], row["type"], row["branch"], row["version"]): row for row in metadata.values()}
    for entry in get_firmware_versions(soc=soc):
        identity = release_identity(entry.version, entry.branch)
        if identity is None or (soc.lower() == "esp8266" and identity["release_line"] != "5.0"):
            continue
        if entry.type not in {"debug", "release"}:
            continue
        stored = indexed[(entry.soc, entry.type, entry.branch, entry.version)]
        if stored["release_line"] and stored["release_line"] != identity["release_line"]:
            continue
        if stored["stage"] and stored["stage"] != identity["stage"]:
            continue
        result.append({**entry.model_dump(), **identity, "build_type": entry.type,
                       "id": str(stored["id"]), "sha256": stored["sha256"], "size": stored["size"],
                       "source_sha": stored["source_sha"], "rgbwwled_version": stored["rgbwwled_version"]})
    return sorted(result, key=lambda entry: version_sort_key(entry["version"]), reverse=True)


@app.get("/api/v2/firmware/options", tags=["Version-aware Firmware Discovery"])
def firmware_options(soc: str, release_line: Optional[str] = None, build_type: Optional[str] = None):
    candidates = firmware_candidates(soc)
    if build_type:
        candidates = [entry for entry in candidates if entry["build_type"] == build_type]
    if release_line:
        candidates = [entry for entry in candidates if entry["release_line"] == release_line]
    combinations = sorted({(entry["release_line"], entry["stage"], entry["build_type"]) for entry in candidates},
                          key=lambda value: (tuple(map(int, value[0].split("."))), value[1], value[2]))
    return {"soc": soc.lower(), "combinations": [dict(zip(("release_line", "stage", "build_type"), values)) for values in combinations]}


@app.get("/api/v2/firmware/versions", tags=["Version-aware Firmware Discovery"])
def firmware_versions_v2(soc: str, release_line: str, stage: str, build_type: str):
    if not re.fullmatch(r"\d+\.\d+", release_line) or build_type not in {"debug", "release"}:
        raise HTTPException(status_code=422, detail="Invalid release line or build type")
    stage = {"test": "testing", "stable": "prod"}.get(stage, stage)
    if stage not in {"develop", "testing", "prod"}:
        raise HTTPException(status_code=422, detail="Invalid stage")
    return [entry for entry in firmware_candidates(soc) if entry["release_line"] == release_line
            and entry["stage"] == stage and entry["build_type"] == build_type]


@app.get("/api/v2/firmware/latest", tags=["Version-aware Firmware Discovery"])
def firmware_latest_v2(soc: str, release_line: str, stage: str, build_type: str):
    candidates = firmware_versions_v2(soc, release_line, stage, build_type)
    if not candidates:
        raise HTTPException(status_code=404, detail="No firmware matching requested parameters")
    return candidates[0]


@app.get("/api/v2/firmware/transition", tags=["Version-aware Firmware Discovery"])
def firmware_transition(soc: str, installed_version: str, target_id: str, installed_webapp_version: str = ""):
    source = re.match(r"^[Vv]?(\d+\.\d+)(?:\.|-|$)", installed_version)
    target = next((entry for entry in firmware_candidates(soc) if entry["id"] == target_id), None)
    if source is None or target is None:
        raise HTTPException(status_code=422, detail="Installed identity or target release is unavailable")
    source_line = tuple(int(component) for component in source[1].split("."))
    target_line = tuple(int(component) for component in target["release_line"].split("."))
    if target_line < source_line:
        return {"status": "blocked", "reason": "Cross-line downgrades require a ConfigDB schema compatibility review", "steps": []}
    return {"status": "ready", "steps": ["install_firmware"]}


def validate_path_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or ".." in value:
        raise HTTPException(status_code=422, detail="Invalid artifact path component")
    return value


def validate_branch_path(branch: str) -> str:
    for component in branch.split("/"):
        validate_path_component(component)
    return branch


# ---------------------------------------------------------------------------
# CI Upload Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/firmware/upload", status_code=201, tags=["CI Upload"])
async def upload_firmware(
    soc: str = Form(...),
    type: str = Form(...),
    branch: str = Form(...),
    version: str = Form(...),
    signature: str = Form(...),
    comment: str = Form(""),
    file: UploadFile = File(...),
    _: None = Depends(verify_token),
    release_line: str = Form(""),
    stage: str = Form(""),
    source_sha: str = Form(""),
    rgbwwled_version: str = Form(""),
    upgrade_from_lines: str = Form("[]"),
):
    soc, type, branch = soc.lower(), type.lower(), branch.lower()
    identity = release_identity(version, branch)
    if identity is None or type not in {"debug", "release"} or soc not in {"esp8266", "esp32", "esp32c3"}:
        raise HTTPException(status_code=422, detail="Invalid firmware identity")
    if release_line and release_line != identity["release_line"]:
        raise HTTPException(status_code=422, detail="Release line does not match version")
    normalized_stage = {"test": "testing", "stable": "prod"}.get(stage, stage)
    if normalized_stage and normalized_stage != identity["stage"]:
        raise HTTPException(status_code=422, detail="Stage does not match branch")
    if soc == "esp8266" and identity["release_line"] != "5.0":
        raise HTTPException(status_code=422, detail="Unsupported release line for ESP8266")
    expected_component = {"5.0": "0.10.1", "5.1": "0.11.0"}.get(identity["release_line"])
    if rgbwwled_version and expected_component and rgbwwled_version != expected_component:
        raise HTTPException(status_code=422, detail="Incorrect RGBWWLed version for release line")
    try:
        qualified_sources = json.loads(upgrade_from_lines)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid qualified source lines")
    if not isinstance(qualified_sources, list) or not all(isinstance(line, str) and re.fullmatch(r"\d+\.\d+", line) for line in qualified_sources):
        raise HTTPException(status_code=422, detail="Invalid qualified source lines")
    validate_branch_path(branch)
    validate_path_component(version)
    validate_path_component(file.filename or "")
    payload = await file.read()
    verify_sig(payload, signature)
    checksum = hashlib.sha256(payload).hexdigest()

    target_dir = STATIC_ROOT / "firmware" / branch / version / soc / type
    target_dir.mkdir(parents=True, exist_ok=True)

    file_path = target_dir / file.filename
    with get_db_connection() as connection:
        existing = connection.execute("SELECT sha256, url FROM firmware_builds WHERE soc = ? AND type = ? AND branch = ? AND version = ?", (soc, type, branch, version)).fetchone()
    if existing:
        if existing["sha256"] != checksum:
            raise HTTPException(status_code=409, detail="Release already exists with different or unverified content")
        return {"status": "success", "url": existing["url"]}
    if file_path.exists() and hashlib.sha256(file_path.read_bytes()).hexdigest() != checksum:
        raise HTTPException(status_code=409, detail="Artifact path already contains different content")
    with open(file_path, "wb") as f:
        f.write(payload)

    rel_url = f"/downloads/firmware/{branch}/{version}/{soc}/{type}/{file.filename}"
    full_url = f"{DOWNLOAD_BASE_URL}{rel_url}"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO firmware_builds (soc, type, branch, version, file_path, url, comment,
                release_line, stage, sha256, size, source_sha, rgbwwled_version, upgrade_from_lines)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (soc, type, branch, version, str(file_path), full_url, comment,
             identity["release_line"], identity["stage"], checksum, len(payload), source_sha, rgbwwled_version, json.dumps(qualified_sources)),
        )
        conn.commit()

    return {"status": "success", "url": full_url}


@app.post("/api/webapp/upload", status_code=201, tags=["CI Upload"])
async def upload_webapp(
    branch: str = Form(...),
    version: str = Form(...),
    type: str = Form("debug"),
    min_firmware: str = Form(""),
    signature: str = Form(...),
    comment: str = Form(""),
    archive: UploadFile = File(...),
    _: None = Depends(verify_token),
    max_firmware: str = Form(""),
    api_min: str = Form(""),
    api_max: str = Form(""),
    required_capabilities: str = Form("[]"),
):
    branch = validate_branch_path(branch.lower())
    validate_path_component(version)
    try:
        required = json.loads(required_capabilities)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid webapp capability requirements")
    if not isinstance(required, list) or not all(isinstance(value, str) for value in required):
        raise HTTPException(status_code=422, detail="Invalid webapp capability requirements")
    if max_firmware and (not min_firmware or parse_version_components(max_firmware) <= parse_version_components(min_firmware)):
        raise HTTPException(status_code=422, detail="Invalid webapp firmware compatibility range")
    payload = await archive.read()
    verify_sig(payload, signature)
    checksum = hashlib.sha256(payload).hexdigest()
    target_dir = STATIC_ROOT / "webapps" / branch / version
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    basepath = f"{DOWNLOAD_BASE_URL}/downloads/webapps/{branch}/{version}/"
    with tempfile.TemporaryDirectory(dir=target_dir.parent) as temporary:
        staging = Path(temporary) / "assets"
        staging.mkdir()
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as bundle:
                if any(not (member.isfile() or member.isdir()) for member in bundle.getmembers()):
                    raise HTTPException(status_code=422, detail="Webapp archive must not contain links or special files")
                bundle.extractall(path=staging, filter="data")
        except (tarfile.TarError, OSError) as error:
            raise HTTPException(status_code=400, detail="Invalid webapp archive") from error
        if not any((staging / name).is_file() for name in ("index.html", "index.html.gz")):
            raise HTTPException(status_code=422, detail="Webapp archive has no root entry point")
        with get_db_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM webapp_builds WHERE branch = ? AND version = ?", (branch, version)).fetchone()
            if existing:
                unchanged = existing["sha256"] == checksum and all((existing[key] or "") == value for key, value in
                    (("min_firmware", min_firmware), ("max_firmware", max_firmware), ("api_min", api_min), ("api_max", api_max)))
                unchanged = unchanged and json.loads(existing["required_capabilities"] or "[]") == required
                if not unchanged:
                    raise HTTPException(status_code=409, detail="Webapp release is immutable; publish a new version")
                return {"status": "success", "basepath": existing["basepath"]}
            if target_dir.exists():
                raise HTTPException(status_code=409, detail="Webapp artifact directory already exists")
            staging.rename(target_dir)
            try:
                connection.execute("""INSERT INTO webapp_builds
                    (branch, version, type, min_firmware, max_firmware, api_min, api_max,
                     required_capabilities, sha256, file_path, basepath, comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (branch, version, type.lower(), min_firmware, max_firmware, api_min, api_max,
                     json.dumps(required), checksum, str(target_dir), basepath, comment))
                connection.commit()
            except Exception:
                shutil.rmtree(target_dir)
                raise
    return {"status": "success", "basepath": basepath}


# ---------------------------------------------------------------------------
# Client Read API
# ---------------------------------------------------------------------------

@app.get("/api/firmware/socs", response_model=List[str], tags=["Firmware Discovery"])
def get_firmware_socs(branch: Optional[str] = None, type: Optional[str] = None):
    query = "SELECT DISTINCT soc FROM firmware_builds WHERE 1=1"
    params = []
    if branch:
        query += " AND branch = ?"
        params.append(branch.lower())
    if type:
        query += " AND type = ?"
        params.append(type.lower())

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return sorted([row["soc"] for row in rows])


@app.get("/api/firmware/branches", response_model=List[str], tags=["Firmware Discovery"])
def get_firmware_branches(soc: Optional[str] = None, type: Optional[str] = None):
    query = "SELECT DISTINCT branch FROM firmware_builds WHERE 1=1"
    params = []
    if soc:
        query += " AND soc = ?"
        params.append(soc.lower())
    if type:
        query += " AND type = ?"
        params.append(type.lower())

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return sorted([row["branch"] for row in rows])


@app.get("/api/firmware/types", response_model=List[str], tags=["Firmware Discovery"])
def get_firmware_types(soc: Optional[str] = None, branch: Optional[str] = None):
    query = "SELECT DISTINCT type FROM firmware_builds WHERE 1=1"
    params = []
    if soc:
        query += " AND soc = ?"
        params.append(soc.lower())
    if branch:
        query += " AND branch = ?"
        params.append(branch.lower())

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return sorted([row["type"] for row in rows])


@app.get("/api/firmware/versions", response_model=List[FirmwareEntry], tags=["Firmware Discovery"])
def get_firmware_versions(soc: Optional[str] = None, branch: Optional[str] = None, type: Optional[str] = None):
    query = "SELECT soc, type, branch, version, url, COALESCE(comment, '') AS comment FROM firmware_builds WHERE 1=1"
    params = []
    if soc:
        query += " AND soc = ?"
        params.append(soc.lower())
    if branch:
        query += " AND branch = ?"
        params.append(branch.lower())
    if type:
        query += " AND type = ?"
        params.append(type.lower())

    with get_db_connection() as conn:
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]

    rows.sort(key=lambda x: parse_version_components(x["version"]), reverse=True)
    return [FirmwareEntry(**r) for r in rows]


@app.get("/api/firmware/latest", response_model=Union[FirmwareEntry, List[FirmwareEntry]], tags=["Firmware Discovery"])
def get_latest_firmware(soc: Optional[str] = None, branch: Optional[str] = None, type: Optional[str] = None):
    versions = get_firmware_versions(soc, branch, type)
    if not versions:
        raise HTTPException(status_code=404, detail="No firmware matching requested parameters")

    if soc and branch and type:
        return versions[0]

    groups: dict[tuple[str, str, str], FirmwareEntry] = {}
    for entry in versions:
        key = (entry.soc, entry.branch, entry.type)
        if key not in groups:
            groups[key] = entry
    return list(groups.values())


@app.get("/api/webapp/branches", response_model=List[str], tags=["Webapp Discovery"])
def get_webapp_branches():
    with get_db_connection() as conn:
        rows = conn.execute("SELECT DISTINCT branch FROM webapp_builds").fetchall()
    return sorted([row["branch"] for row in rows])


@app.get("/api/webapp/versions", response_model=List[WebappEntry], tags=["Webapp Discovery"])
def get_webapp_versions(branch: Optional[str] = None, firmware_version: Optional[str] = None):
    query = "SELECT branch, version, type, basepath, COALESCE(min_firmware, '') AS min_firmware, COALESCE(comment, '') AS comment FROM webapp_builds WHERE 1=1"
    params = []
    if branch:
        query += " AND branch = ?"
        params.append(branch.lower())

    with get_db_connection() as conn:
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]

    if firmware_version:
        fw_comp = parse_version_components(firmware_version)
        rows = [
            r for r in rows
            if not r["min_firmware"] or fw_comp >= parse_version_components(r["min_firmware"])
        ]

    rows.sort(key=lambda x: parse_version_components(x["version"]), reverse=True)
    return [WebappEntry(**r) for r in rows]


def canonical_stage(value: str) -> str:
    suffix = re.split(r"[/]", value.lower())[-1]
    qualified = re.fullmatch(r"\d+\.\d+-(develop|testing|test|stable|prod|experimental)", suffix)
    if qualified:
        suffix = qualified[1]
    return {"develop": "develop", "experimental": "develop", "testing": "testing", "test": "testing", "stable": "prod", "prod": "prod"}.get(suffix, "")


def webapp_manifest(record: dict) -> dict:
    directory = Path(record["file_path"])
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="Webapp files are unavailable")
    files = []
    for asset in sorted(directory.rglob("*")):
        if asset.is_symlink():
            raise HTTPException(status_code=409, detail="Webapp contains unsupported links")
        if asset.is_file():
            payload = asset.read_bytes()
            files.append({"filename": asset.relative_to(directory).as_posix(),
                          "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
                          "size": len(payload)})
    if not files:
        raise HTTPException(status_code=404, detail="Webapp has no installable files")
    return {"version": record["version"], "branch": record["branch"], "basepath": record["basepath"],
            "files": files, "total_size": sum(asset["size"] for asset in files)}


@app.get("/api/v2/webapp/versions", tags=["Version-aware Webapp Discovery"])
def webapp_versions_v2(firmware_version: str, stage: str, api_version: str = "", capabilities: str = "", asset_budget: Optional[int] = None):
    requested_stage = canonical_stage(stage)
    firmware_line = re.match(r"^[Vv]?(\d+\.\d+)(?:\.|-|$)", firmware_version)
    if not requested_stage or firmware_line is None:
        raise HTTPException(status_code=422, detail="Firmware identity and stage are required")
    with get_db_connection() as connection:
        records = [dict(row) for row in connection.execute("SELECT * FROM webapp_builds")]
    result = []
    for record in records:
        identity = release_identity(record["version"], record["branch"])
        if identity is None or identity["stage"] != requested_stage or identity["release_line"] != firmware_line[1]:
            continue
        minimum = record["min_firmware"] or ""
        maximum = record["max_firmware"] or ""
        if minimum and parse_version_components(firmware_version) < parse_version_components(minimum):
            continue
        if maximum and parse_version_components(firmware_version) >= parse_version_components(maximum):
            continue
        if record["api_min"] or record["api_max"]:
            if not api_version:
                continue
            if record["api_min"] and version_sort_key(api_version) < version_sort_key(record["api_min"]):
                continue
            if record["api_max"] and version_sort_key(api_version) >= version_sort_key(record["api_max"]):
                continue
        try:
            required = json.loads(record["required_capabilities"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(required, list) or not all(isinstance(value, str) for value in required):
            continue
        if not set(required).issubset(set(filter(None, capabilities.split(",")))):
            continue
        try:
            manifest = webapp_manifest(record)
        except HTTPException:
            continue
        if asset_budget is not None and manifest["total_size"] > asset_budget:
            continue
        result.append({**manifest, "id": str(record["id"]), "stage": requested_stage,
                       "min_firmware": minimum, "max_firmware": maximum,
                       "required_capabilities": required})
    return sorted(result, key=lambda record: version_sort_key(record["version"]), reverse=True)


@app.get("/api/v2/webapp/options", tags=["Version-aware Webapp Discovery"])
def webapp_options_v2(firmware_version: str, api_version: str = "", capabilities: str = "", asset_budget: Optional[int] = None):
    return {"stages": [stage for stage in ("develop", "testing", "prod")
                       if webapp_versions_v2(firmware_version, stage, api_version, capabilities, asset_budget)]}


@app.get("/api/v2/webapp/latest", tags=["Version-aware Webapp Discovery"])
def webapp_latest_v2(firmware_version: str, stage: str, api_version: str = "", capabilities: str = "", asset_budget: Optional[int] = None):
    candidates = webapp_versions_v2(firmware_version, stage, api_version, capabilities, asset_budget)
    if not candidates:
        raise HTTPException(status_code=404, detail="No compatible webapp; existing installation remains valid")
    return candidates[0]


@app.get("/api/webapp/latest", tags=["Webapp Discovery"])
def get_latest_webapp(branch: str, firmware_version: str):
    stage = canonical_stage(branch)
    if not stage and re.fullmatch(r"\d+\.\d+", branch):
        suffix = re.sub(r"^[Vv]?\d+\.\d+(?:\.\d+)?-\d+-", "", firmware_version)
        stage = canonical_stage(suffix)
    candidate = webapp_latest_v2(firmware_version, stage)
    return {key: candidate[key] for key in ("version", "branch", "basepath", "files", "total_size")}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("version_api:app", host=LISTEN_HOST, port=LISTEN_PORT, reload=False)

