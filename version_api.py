#!/usr/bin/env python3
"""
Lightinator Release & Distribution Service
Provides secure upload endpoints for CI deployment and read-only version discovery for clients.
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Union

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

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
):
    payload = await file.read()
    verify_sig(payload, signature)

    target_dir = STATIC_ROOT / "firmware" / branch / version / soc / type
    target_dir.mkdir(parents=True, exist_ok=True)

    file_path = target_dir / file.filename
    with open(file_path, "wb") as f:
        f.write(payload)

    rel_url = f"/downloads/firmware/{branch}/{version}/{soc}/{type}/{file.filename}"
    full_url = f"{DOWNLOAD_BASE_URL}{rel_url}"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO firmware_builds (soc, type, branch, version, file_path, url, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(soc, branch, type, version) DO UPDATE SET
                file_path=excluded.file_path,
                url=excluded.url,
                comment=excluded.comment,
                created_at=CURRENT_TIMESTAMP
            """,
            (soc.lower(), type.lower(), branch.lower(), version, str(file_path), full_url, comment),
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
):
    payload = await archive.read()
    verify_sig(payload, signature)

    target_dir = STATIC_ROOT / "webapps" / branch / version
    target_dir.mkdir(parents=True, exist_ok=True)

    archive_path = target_dir / archive.filename
    with open(archive_path, "wb") as f:
        f.write(payload)

    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as tar:
            tar.extractall(path=target_dir, filter="data")
        archive_path.unlink()
    else:
        archive_path.unlink()
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid tar archive")

    basepath = f"{DOWNLOAD_BASE_URL}/downloads/webapps/{branch}/{version}/"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO webapp_builds (branch, version, type, min_firmware, file_path, basepath, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch, version) DO UPDATE SET
                type=excluded.type,
                min_firmware=excluded.min_firmware,
                file_path=excluded.file_path,
                basepath=excluded.basepath,
                comment=excluded.comment,
                created_at=CURRENT_TIMESTAMP
            """,
            (branch.lower(), version, type.lower(), min_firmware, str(target_dir), basepath, comment),
        )
        conn.commit()

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
    query = "SELECT soc, type, branch, version, url, comment FROM firmware_builds WHERE 1=1"
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
    query = "SELECT branch, version, type, basepath, min_firmware, comment FROM webapp_builds WHERE 1=1"
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("version_api:app", host=LISTEN_HOST, port=LISTEN_PORT, reload=False)

