#!/usr/bin/env python3
"""
Lightinator Release & Distribution Service
Provides secure upload endpoints for CI deployment and read-only version discovery for clients.
"""

from __future__ import annotations

import os
import hashlib
import io
import logging
import hmac
import json
import re
import sqlite3
import tarfile
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, List, Literal, Optional, Union
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Configuration & Environment
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("VERSION_DB_PATH", "firmware.db")
STATIC_ROOT = Path(os.getenv("VERSION_HTML_ROOT", "/var/www/downloads"))
OTA_STORAGE_ROOT = Path(os.getenv("OTA_STORAGE_ROOT", "/var/lib/lightinator/ota-artifacts"))
DOWNLOAD_BASE_URL = os.getenv("VERSION_DOWNLOAD_BASE_URL", "http://lightinator.de").rstrip("/")
CI_TOKEN = os.getenv("CI_UPLOAD_TOKEN", "")
OTA_PROMOTION_TOKEN = os.getenv("OTA_PROMOTION_TOKEN", "")
OTA_ADMIN_TOKEN = os.getenv("OTA_ADMIN_TOKEN", "")
PUBLIC_KEY_HEX = os.getenv("RELEASE_PUBLIC_KEY_HEX", "")
PUBLISHING_API_ENABLED = os.getenv("ENABLE_PUBLISHING_API", "0") == "1"
LEGACY_CATALOG_PATH = os.getenv("VERSION_LEGACY_CATALOG", "")
LEGACY_ASSET_ROOT = os.getenv("VERSION_LEGACY_ASSET_ROOT", "")
_legacy_catalogue_stamp = None
MAX_OTA_ARTIFACT_SIZE = 64 * 1024 * 1024
MAX_OTA_RELEASE_SIZE = 512 * 1024 * 1024
DEFAULT_OTA_PRODUCT = "controller"
DEFAULT_CONTROLLER_TARGET_POLICY = {
    "5.0": ["esp8266", "esp32", "esp32c3"],
    "5.1": ["esp32", "esp32c3"],
}
OTA_CHANNEL_RE = re.compile(r"^(\d+\.\d+)/(develop|testing|prod)$")
OTA_PRODUCT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
OTA_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OTA_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OTA_VERSION_RE = re.compile(r"^[Vv]?(\d+\.\d+)\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

LISTEN_HOST = os.getenv("VERSION_API_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("VERSION_API_PORT", "8765"))
OPENAPI_URL = "/api/openapi.json"
DISTRIBUTION_ICON_URL = "/api/brand/lightinator-distribution-server-icon-dark.svg"


# ---------------------------------------------------------------------------
# Database Helper & Initialization
# ---------------------------------------------------------------------------

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
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
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ota_products (
                product_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                publisher_repository TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                signing_public_key TEXT NOT NULL DEFAULT '',
                target_policy_json TEXT NOT NULL
            );
            """
        )
        firmware_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(firmware_builds)")}
        for name, kind in {"release_line": "TEXT", "stage": "TEXT", "sha256": "TEXT", "size": "INTEGER",
                           "source_sha": "TEXT", "rgbwwled_version": "TEXT", "upgrade_from_lines": "TEXT"}.items():
            if name not in firmware_columns:
                cursor.execute(f"ALTER TABLE firmware_builds ADD COLUMN {name} {kind}")
        webapp_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(webapp_builds)")}
        for name in ("max_firmware", "api_min", "api_max", "required_capabilities", "sha256", "files_json"):
            if name not in webapp_columns:
                cursor.execute(f"ALTER TABLE webapp_builds ADD COLUMN {name} TEXT")
        product_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(ota_products)")}
        if "publisher_repository" not in product_columns:
            cursor.execute("ALTER TABLE ota_products ADD COLUMN publisher_repository TEXT NOT NULL DEFAULT ''")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ota_releases (
                release_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES ota_products(product_id),
                version TEXT NOT NULL,
                origin_channel TEXT NOT NULL,
                source_repository TEXT NOT NULL,
                source_commit TEXT NOT NULL,
                signing_public_key TEXT NOT NULL,
                signature_hex TEXT NOT NULL,
                changelog_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('staged', 'published')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(product_id, version)
            );
            """
        )
        release_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(ota_releases)")}
        if "signing_public_key" not in release_columns:
            cursor.execute("ALTER TABLE ota_releases ADD COLUMN signing_public_key TEXT NOT NULL DEFAULT ''")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ota_artifacts (
                artifact_id TEXT NOT NULL,
                release_id TEXT NOT NULL REFERENCES ota_releases(release_id) ON DELETE CASCADE,
                hardware TEXT NOT NULL,
                hardware_revision TEXT NOT NULL DEFAULT '',
                soc TEXT NOT NULL,
                build_type TEXT NOT NULL,
                role TEXT NOT NULL,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL CHECK (size > 0),
                storage_path TEXT,
                uploaded INTEGER NOT NULL DEFAULT 0 CHECK (uploaded IN (0, 1)),
                PRIMARY KEY(release_id, artifact_id),
                UNIQUE(release_id, hardware, hardware_revision, soc, build_type, role)
            );
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ota_release_channels (
                release_id TEXT NOT NULL REFERENCES ota_releases(release_id) ON DELETE CASCADE,
                channel TEXT NOT NULL,
                promoted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(release_id, channel)
            );
            """
        )
        cursor.execute(
            """
            INSERT INTO ota_products
                (product_id, display_name, publisher_repository, enabled, signing_public_key, target_policy_json)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                signing_public_key = CASE
                    WHEN ota_products.signing_public_key = '' THEN excluded.signing_public_key
                    ELSE ota_products.signing_public_key
                END,
                publisher_repository = CASE
                    WHEN ota_products.publisher_repository = '' THEN excluded.publisher_repository
                    ELSE ota_products.publisher_repository
                END
            """,
            (
                DEFAULT_OTA_PRODUCT,
                "Lightinator Controller",
                "pljakobs/esp_rgbww_firmware",
                PUBLIC_KEY_HEX.lower(),
                json.dumps(DEFAULT_CONTROLLER_TARGET_POLICY, sort_keys=True),
            ),
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
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

app.mount(
        "/api/brand",
        StaticFiles(directory=Path(__file__).parent / "images"),
        name="brand",
)


@app.middleware("http")
async def publishing_api_gate(request: Request, call_next):
    if not PUBLISHING_API_ENABLED and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse(status_code=404, content={"detail": "HTTP publishing is disabled; use manage_version.py"})
    return await call_next(request)


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
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not CI_TOKEN or not hmac.compare_digest(token, CI_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing authentication token")


def verify_promotion_token(authorization: str = Header(...)) -> None:
    scheme, separator, token = authorization.partition(" ")
    if (
        not OTA_PROMOTION_TOKEN
        or not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(token.strip(), OTA_PROMOTION_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing promotion authorization")


def verify_ota_admin_token(authorization: str = Header(...)) -> None:
    scheme, separator, token = authorization.partition(" ")
    if (
        not OTA_ADMIN_TOKEN
        or not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(token.strip(), OTA_ADMIN_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing OTA admin authorization")


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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _safe_segment(value: str) -> bool:
    return bool(OTA_SEGMENT_RE.fullmatch(value)) and value not in {".", ".."}


def _manifest_version_line(version: str) -> Optional[str]:
    match = OTA_VERSION_RE.fullmatch(version)
    return match.group(1) if match else None


class OTAChangelogEntry(BaseModel):
    commit: str = Field(min_length=7, max_length=64)
    subject: str = Field(min_length=1, max_length=512)

    class Config:
        extra = "forbid"


class OTAArtifactManifest(BaseModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=128)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(gt=0, le=MAX_OTA_ARTIFACT_SIZE)

    class Config:
        extra = "forbid"


class OTATargetManifest(BaseModel):
    hardware: str = Field(min_length=1, max_length=128)
    hardware_revision: str = Field(default="", max_length=64)
    soc: str = Field(min_length=1, max_length=32)
    build_type: Literal["debug", "release"]
    artifacts: List[OTAArtifactManifest] = Field(min_items=1)

    class Config:
        extra = "forbid"


class OTAReleaseManifest(BaseModel):
    release_id: str = Field(min_length=36, max_length=36)
    product: str = Field(default=DEFAULT_OTA_PRODUCT, min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=128)
    channel: str = Field(min_length=1, max_length=96)
    source_repository: str = Field(min_length=1, max_length=256)
    source_commit: str = Field(min_length=40, max_length=64)
    changelog: List[OTAChangelogEntry] = Field(min_length=1, max_length=200)
    targets: List[OTATargetManifest] = Field(min_items=1, max_items=64)
    upgrade_from_lines: Optional[List[str]] = Field(default=None, max_length=20)

    class Config:
        extra = "forbid"


class OTAReleaseCreateRequest(BaseModel):
    manifest: OTAReleaseManifest
    signature: str = Field(min_length=128, max_length=128)

    class Config:
        extra = "forbid"


class OTAPromoteRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=96)


class OTAProductRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    publisher_repository: str = Field(min_length=3, max_length=256)
    signing_public_key: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    target_policy: dict[str, List[str]]


def _enabled_ota_product(product_id: str) -> sqlite3.Row:
    with get_db_connection() as conn:
        product = conn.execute(
            "SELECT * FROM ota_products WHERE product_id = ? AND enabled = 1",
            (product_id.lower(),),
        ).fetchone()
    if product is None:
        raise HTTPException(status_code=404, detail="OTA product not found")
    return product


def _validate_target_policy(target_policy: dict[str, List[str]]) -> dict[str, List[str]]:
    if not target_policy:
        raise HTTPException(status_code=422, detail="target_policy must contain at least one version line")
    normalized: dict[str, List[str]] = {}
    for version_line, socs in target_policy.items():
        if not re.fullmatch(r"\d+\.\d+", version_line) or not socs:
            raise HTTPException(status_code=422, detail="target_policy keys must be version lines with non-empty SoC lists")
        normalized_socs = sorted({soc.lower() for soc in socs})
        if any(not _safe_segment(soc) for soc in normalized_socs):
            raise HTTPException(status_code=422, detail="Invalid SoC in target_policy")
        normalized[version_line] = normalized_socs
    return normalized


def _validate_manifest_targets(product: sqlite3.Row, version_line: str, manifest: OTAReleaseManifest) -> None:
    if any(not re.fullmatch(r"\d+\.\d+", line) for line in (manifest.upgrade_from_lines or [])):
        raise HTTPException(status_code=422, detail="Qualified upgrade sources must be major.minor lines")
    target_policy = json.loads(product["target_policy_json"])
    allowed_socs = {soc.lower() for soc in target_policy.get(version_line, [])}
    if not allowed_socs:
        raise HTTPException(status_code=422, detail="Product is not enabled for this version line")
    unsupported = sorted({target.soc.lower() for target in manifest.targets} - allowed_socs)
    if unsupported:
        raise HTTPException(status_code=422, detail=f"Unsupported SoC for product/version line: {', '.join(unsupported)}")


@app.get("/api/ota/products", tags=["OTA Release Catalog"])
@app.get("/api/products", tags=["Product Discovery"])
def list_ota_products():
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT product_id, display_name, publisher_repository, target_policy_json "
            "FROM ota_products WHERE enabled = 1 ORDER BY product_id"
        ).fetchall()
    return [
        {
            "product": row["product_id"],
            "display_name": row["display_name"],
            "publisher_repository": row["publisher_repository"],
            "target_policy": json.loads(row["target_policy_json"]),
        }
        for row in rows
    ]


@app.post("/api/ota/products", status_code=201, tags=["OTA Administration"])
def register_ota_product(
    registration: OTAProductRegistration,
    _: None = Depends(verify_ota_admin_token),
):
    if not OTA_PRODUCT_RE.fullmatch(registration.product_id):
        raise HTTPException(status_code=422, detail="Invalid product identifier")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", registration.publisher_repository):
        raise HTTPException(status_code=422, detail="publisher_repository must be owner/repository")
    normalized_policy = _validate_target_policy(registration.target_policy)
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO ota_products
                    (product_id, display_name, publisher_repository, signing_public_key, target_policy_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    registration.product_id.lower(),
                    registration.display_name.strip(),
                    registration.publisher_repository.lower(),
                    registration.signing_public_key.lower(),
                    json.dumps(normalized_policy, sort_keys=True),
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="OTA product already exists") from exc
    return {"status": "registered", "product": registration.product_id.lower()}


@app.post("/api/ota/releases", status_code=201, tags=["OTA Release Upload"])
def create_ota_release(
    request: OTAReleaseCreateRequest,
    _: None = Depends(verify_token),
):
    manifest = request.manifest
    release_id, version_line = _validate_ota_manifest(manifest)
    product = _enabled_ota_product(manifest.product)
    if manifest.source_repository.lower() != product["publisher_repository"].lower():
        raise HTTPException(status_code=403, detail="Repository is not authorized to publish this product")
    _validate_manifest_targets(product, version_line, manifest)
    manifest_hash = _verify_ota_manifest_signature(product, manifest, request.signature)
    manifest_json = _canonical_json(manifest.model_dump(mode="json", exclude_none=True)).decode("utf-8")
    changelog_json = json.dumps([entry.model_dump(mode="json") for entry in manifest.changelog], sort_keys=True)

    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO ota_releases
                    (release_id, product_id, version, origin_channel, source_repository,
                     source_commit, signing_public_key, signature_hex, changelog_json, manifest_json,
                     manifest_sha256, status)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged')
                """,
                (
                    release_id,
                    manifest.product.lower(),
                    manifest.version,
                    manifest.channel,
                    manifest.source_repository.lower(),
                    manifest.source_commit.lower(),
                    product["signing_public_key"].lower(),
                    request.signature.lower(),
                    changelog_json,
                    manifest_json,
                    manifest_hash,
                ),
            )
            for target in manifest.targets:
                for artifact in target.artifacts:
                    conn.execute(
                        """
                        INSERT INTO ota_artifacts
                            (release_id, artifact_id, hardware, hardware_revision, soc,
                             build_type, role, filename, sha256, size)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            release_id,
                            artifact.artifact_id,
                            target.hardware,
                            target.hardware_revision,
                            target.soc.lower(),
                            target.build_type,
                            artifact.role,
                            artifact.filename,
                            artifact.sha256.lower(),
                            artifact.size,
                        ),
                    )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Release ID or product/version already exists") from exc

    return {
        "release_id": release_id,
        "status": "staged",
        "manifest_sha256": manifest_hash,
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "upload_url": f"/api/ota/releases/{release_id}/artifacts/{artifact.artifact_id}",
            }
            for target in manifest.targets
            for artifact in target.artifacts
        ],
    }


def _validate_ota_manifest(manifest: OTAReleaseManifest) -> tuple[str, str]:
    if not OTA_PRODUCT_RE.fullmatch(manifest.product):
        raise HTTPException(status_code=422, detail="Invalid product identifier")
    try:
        release_id = str(uuid.UUID(manifest.release_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="release_id must be a UUID") from exc
    if release_id != manifest.release_id.lower():
        raise HTTPException(status_code=422, detail="release_id must use canonical UUID format")

    channel_match = OTA_CHANNEL_RE.fullmatch(manifest.channel)
    version_line = _manifest_version_line(manifest.version)
    if channel_match is None or version_line is None or channel_match.group(1) != version_line:
        raise HTTPException(status_code=422, detail="version must match the version line in channel")
    if channel_match.group(2) == "prod":
        raise HTTPException(status_code=422, detail="prod channel membership requires the promotion API")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", manifest.source_commit):
        raise HTTPException(status_code=422, detail="source_commit must be a full Git object ID")

    artifact_ids: set[str] = set()
    target_keys: set[tuple[str, str, str, str]] = set()
    total_size = 0
    for target in manifest.targets:
        if not all(_safe_segment(value) for value in (target.hardware, target.soc, target.build_type)):
            raise HTTPException(status_code=422, detail="Invalid target identifier")
        if target.hardware_revision and not _safe_segment(target.hardware_revision):
            raise HTTPException(status_code=422, detail="Invalid hardware revision")
        target_key = (target.hardware, target.hardware_revision, target.soc.lower(), target.build_type)
        if target_key in target_keys:
            raise HTTPException(status_code=422, detail="Duplicate target in manifest")
        target_keys.add(target_key)
        if not target.artifacts:
            raise HTTPException(status_code=422, detail="Each target must declare artifacts")
        for artifact in target.artifacts:
            if not OTA_ARTIFACT_ID_RE.fullmatch(artifact.artifact_id) or not _safe_segment(artifact.role):
                raise HTTPException(status_code=422, detail="Invalid artifact identifier or role")
            if artifact.artifact_id in artifact_ids:
                raise HTTPException(status_code=422, detail="artifact_id values must be unique per release")
            artifact_ids.add(artifact.artifact_id)
            total_size += artifact.size
            if total_size > MAX_OTA_RELEASE_SIZE:
                raise HTTPException(status_code=422, detail="Release artifact total exceeds size limit")
            if Path(artifact.filename).name != artifact.filename or "\\" in artifact.filename or not _safe_segment(artifact.filename):
                raise HTTPException(status_code=422, detail="Invalid artifact filename")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", artifact.sha256):
                raise HTTPException(status_code=422, detail="artifact sha256 must be hexadecimal")
    for entry in manifest.changelog:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", entry.commit):
            raise HTTPException(status_code=422, detail="Changelog commit must be a Git object ID")

    return release_id, version_line


def _verify_ota_manifest_signature(product: sqlite3.Row, manifest: OTAReleaseManifest, signature_hex: str) -> str:
    key_hex = product["signing_public_key"]
    if not key_hex:
        raise HTTPException(status_code=503, detail="Product signing key is not configured")
    try:
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
        canonical_manifest = _canonical_json(manifest.model_dump(mode="json", exclude_none=True))
        signature = bytes.fromhex(signature_hex)
        public_key.verify(signature, canonical_manifest)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Release manifest signature verification failed") from exc
    return hashlib.sha256(canonical_manifest).hexdigest()


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

@app.put("/api/ota/releases/{release_id}/artifacts/{artifact_id}", tags=["OTA Release Upload"])
async def upload_ota_artifact(
    release_id: str,
    artifact_id: str,
    request: Request,
    _: None = Depends(verify_token),
):
    try:
        release_id = str(uuid.UUID(release_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="OTA release not found") from exc
    with get_db_connection() as conn:
        artifact = conn.execute(
            """
            SELECT a.*, r.product_id, r.status AS release_status
            FROM ota_artifacts a JOIN ota_releases r USING (release_id)
            WHERE a.release_id = ? AND a.artifact_id = ?
            """,
            (release_id, artifact_id),
        ).fetchone()
    if artifact is None:
        raise HTTPException(status_code=404, detail="OTA artifact not found")
    if artifact["release_status"] != "staged":
        raise HTTPException(status_code=409, detail="Release is not staged")

    artifact_dir = OTA_STORAGE_ROOT / "staging" / artifact["product_id"] / release_id / artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = artifact_dir / f".{uuid.uuid4().hex}.part"
    size = 0
    digest = hashlib.sha256()
    try:
        with temp_path.open("xb") as stream:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > artifact["size"] or size > MAX_OTA_ARTIFACT_SIZE:
                    raise HTTPException(status_code=413, detail="Artifact exceeds declared size")
                digest.update(chunk)
                stream.write(chunk)
        if size != artifact["size"] or digest.hexdigest() != artifact["sha256"].lower():
            raise HTTPException(status_code=422, detail="Artifact size or SHA-256 does not match manifest")

        staged_path = artifact_dir / artifact["filename"]
        os.replace(temp_path, staged_path)
        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE ota_artifacts SET storage_path = ?, uploaded = 1
                WHERE release_id = ? AND artifact_id = ?
                  AND EXISTS (SELECT 1 FROM ota_releases WHERE release_id = ? AND status = 'staged')
                """,
                (str(staged_path), release_id, artifact_id, release_id),
            )
            if cursor.rowcount != 1:
                staged_path.unlink(missing_ok=True)
                raise HTTPException(status_code=409, detail="Release is no longer staged")
            conn.commit()
    finally:
        temp_path.unlink(missing_ok=True)

    return {"status": "uploaded", "artifact_id": artifact_id, "size": size, "sha256": digest.hexdigest()}


def _ota_release_payload(release: sqlite3.Row, artifacts: List[sqlite3.Row], channels: List[str]) -> dict[str, Any]:
    targets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for artifact in artifacts:
        target_key = (
            artifact["hardware"],
            artifact["hardware_revision"],
            artifact["soc"],
            artifact["build_type"],
        )
        target = targets.setdefault(
            target_key,
            {
                "hardware": artifact["hardware"],
                "hardware_revision": artifact["hardware_revision"],
                "soc": artifact["soc"],
                "build_type": artifact["build_type"],
                "artifacts": [],
            },
        )
        target["artifacts"].append(
            {
                "artifact_id": artifact["artifact_id"],
                "role": artifact["role"],
                "filename": artifact["filename"],
                "sha256": artifact["sha256"],
                "size": artifact["size"],
                "url": f"{DOWNLOAD_BASE_URL}/api/ota/artifacts/{quote(release['release_id'], safe='')}/{quote(artifact['artifact_id'], safe='')}",
            }
        )
    return {
        "status": release["status"],
        "release_id": release["release_id"],
        "product": release["product_id"],
        "version": release["version"],
        "source_channel": release["origin_channel"],
        "channels": channels,
        "manifest": json.loads(release["manifest_json"]),
        "manifest_sha256": release["manifest_sha256"],
        "signature": release["signature_hex"],
        "signing_public_key": release["signing_public_key"],
        "source": {"repository": release["source_repository"], "commit": release["source_commit"]},
        "changelog": json.loads(release["changelog_json"]),
        "targets": list(targets.values()),
    }


@app.post("/api/ota/releases/{release_id}/finalize", tags=["OTA Release Upload"])
def finalize_ota_release(release_id: str, _: None = Depends(verify_token)):
    try:
        release_id = str(uuid.UUID(release_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="OTA release not found") from exc
    with get_db_connection() as conn:
        release = conn.execute("SELECT * FROM ota_releases WHERE release_id = ?", (release_id,)).fetchone()
        if release is None:
            raise HTTPException(status_code=404, detail="OTA release not found")
        artifacts = conn.execute(
            "SELECT * FROM ota_artifacts WHERE release_id = ? ORDER BY artifact_id",
            (release_id,),
        ).fetchall()
        channels = [
            row["channel"]
            for row in conn.execute(
                "SELECT channel FROM ota_release_channels WHERE release_id = ? ORDER BY channel",
                (release_id,),
            ).fetchall()
        ]
    if release["status"] == "published":
        return _ota_release_payload(release, artifacts, channels)
    if not artifacts or any(not artifact["uploaded"] or not artifact["storage_path"] for artifact in artifacts):
        raise HTTPException(status_code=409, detail="All manifest artifacts must be uploaded before finalization")

    staging_root = (OTA_STORAGE_ROOT / "staging").resolve()
    published_root = OTA_STORAGE_ROOT / "published"
    published_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    release_parent = published_root / release["product_id"] / release["version"]
    release_parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    final_dir = release_parent / release_id
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{release_id}-", dir=release_parent))
    moved_dir = False
    try:
        for artifact in artifacts:
            staged_path = Path(artifact["storage_path"]).resolve()
            try:
                staged_path.relative_to(staging_root)
            except ValueError as exc:
                raise HTTPException(status_code=500, detail="Staged artifact path is outside OTA storage") from exc
            if not staged_path.is_file() or staged_path.stat().st_size != artifact["size"]:
                raise HTTPException(status_code=409, detail=f"Staged artifact {artifact['artifact_id']} is missing or changed")
            if hashlib.sha256(staged_path.read_bytes()).hexdigest() != artifact["sha256"].lower():
                raise HTTPException(status_code=409, detail=f"Staged artifact {artifact['artifact_id']} failed hash verification")

            target_dir = temporary_dir / artifact["artifact_id"]
            target_dir.mkdir()
            target_path = target_dir / artifact["filename"]
            shutil.copyfile(staged_path, target_path)
            if hashlib.sha256(target_path.read_bytes()).hexdigest() != artifact["sha256"].lower():
                raise HTTPException(status_code=500, detail="Published artifact copy failed hash verification")

        try:
            os.rename(temporary_dir, final_dir)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="Release publication is already in progress") from exc
        moved_dir = True

        with get_db_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE ota_releases SET status = 'published' WHERE release_id = ? AND status = 'staged'",
                (release_id,),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise HTTPException(status_code=409, detail="Release was finalized concurrently")
            for artifact in artifacts:
                published_path = final_dir / artifact["artifact_id"] / artifact["filename"]
                conn.execute(
                    "UPDATE ota_artifacts SET storage_path = ? WHERE release_id = ? AND artifact_id = ?",
                    (str(published_path), release_id, artifact["artifact_id"]),
                )
            conn.execute(
                "INSERT OR IGNORE INTO ota_release_channels (release_id, channel) VALUES (?, ?)",
                (release_id, release["origin_channel"]),
            )
            conn.commit()
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        if moved_dir:
            shutil.rmtree(final_dir, ignore_errors=True)
        raise

    shutil.rmtree(OTA_STORAGE_ROOT / "staging" / release["product_id"] / release_id, ignore_errors=True)

    with get_db_connection() as conn:
        published_release = conn.execute("SELECT * FROM ota_releases WHERE release_id = ?", (release_id,)).fetchone()
        published_artifacts = conn.execute(
            "SELECT * FROM ota_artifacts WHERE release_id = ? ORDER BY artifact_id",
            (release_id,),
        ).fetchall()
        published_channels = [
            row["channel"]
            for row in conn.execute(
                "SELECT channel FROM ota_release_channels WHERE release_id = ? ORDER BY channel",
                (release_id,),
            ).fetchall()
        ]
    return _ota_release_payload(published_release, published_artifacts, published_channels)


@app.post("/api/ota/releases/{release_id}/promote", tags=["OTA Administration"])
def promote_ota_release(
    release_id: str,
    request: OTAPromoteRequest,
    _: None = Depends(verify_promotion_token),
):
    try:
        release_id = str(uuid.UUID(release_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="OTA release not found") from exc
    channel_match = OTA_CHANNEL_RE.fullmatch(request.channel)
    if channel_match is None:
        raise HTTPException(status_code=422, detail="channel must be <major>.<minor>/(develop|testing|prod)")
    with get_db_connection() as conn:
        release = conn.execute("SELECT * FROM ota_releases WHERE release_id = ?", (release_id,)).fetchone()
        if release is None:
            raise HTTPException(status_code=404, detail="OTA release not found")
        if release["status"] != "published":
            raise HTTPException(status_code=409, detail="Only published releases can be promoted")
        if _manifest_version_line(release["version"]) != channel_match.group(1):
            raise HTTPException(status_code=422, detail="A release can only be promoted within its version line")
        conn.execute(
            "INSERT OR IGNORE INTO ota_release_channels (release_id, channel) VALUES (?, ?)",
            (release_id, request.channel),
        )
        conn.commit()
    return {"status": "promoted", "release_id": release_id, "channel": request.channel}


@app.get("/api/ota/latest", tags=["OTA Discovery"])
def get_latest_ota_release(
    channel: str,
    product: str = DEFAULT_OTA_PRODUCT,
    hardware: str = Query(...),
    hardware_revision: str = "",
    soc: str = Query(...),
    build_type: Literal["debug", "release"] = Query(...),
):
    if OTA_CHANNEL_RE.fullmatch(channel) is None:
        raise HTTPException(status_code=422, detail="channel must be <major>.<minor>/(develop|testing|prod)")
    _enabled_ota_product(product)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.* FROM ota_releases r
            JOIN ota_release_channels c ON c.release_id = r.release_id
            JOIN ota_artifacts a ON a.release_id = r.release_id
            WHERE r.product_id = ? AND c.channel = ? AND r.status = 'published'
              AND a.hardware = ? AND a.hardware_revision = ? AND a.soc = ? AND a.build_type = ?
            """,
            (product.lower(), channel, hardware, hardware_revision, soc.lower(), build_type),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="No published OTA release matches the requested product, channel and target")
        rows.sort(key=lambda row: parse_version_components(row["version"]), reverse=True)
        release = rows[0]
        release_id = release["release_id"]
        artifacts = conn.execute(
            """
            SELECT * FROM ota_artifacts
            WHERE release_id = ? AND hardware = ? AND hardware_revision = ? AND soc = ? AND build_type = ?
            ORDER BY role, artifact_id
            """,
            (release_id, hardware, hardware_revision, soc.lower(), build_type),
        ).fetchall()
        channels = [
            row["channel"]
            for row in conn.execute(
                "SELECT channel FROM ota_release_channels WHERE release_id = ? ORDER BY channel",
                (release_id,),
            ).fetchall()
        ]
    return _ota_release_payload(release, artifacts, channels)


@app.get("/api/ota/artifacts/{release_id}/{artifact_id}", tags=["OTA Discovery"])
def download_ota_artifact(release_id: str, artifact_id: str):
    try:
        release_id = str(uuid.UUID(release_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="OTA artifact not found") from exc
    with get_db_connection() as conn:
        artifact = conn.execute(
            """
            SELECT a.filename, a.storage_path
            FROM ota_artifacts a JOIN ota_releases r USING (release_id)
            WHERE a.release_id = ? AND a.artifact_id = ? AND r.status = 'published'
            """,
            (release_id, artifact_id),
        ).fetchone()
    if artifact is None or not artifact["storage_path"]:
        raise HTTPException(status_code=404, detail="Published OTA artifact not found")
    path = Path(artifact["storage_path"]).resolve()
    try:
        path.relative_to((OTA_STORAGE_ROOT / "published").resolve())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Published artifact path is outside OTA storage") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Published OTA artifact file is missing")
    return FileResponse(path, filename=artifact["filename"], media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# CI Upload Endpoints
# ---------------------------------------------------------------------------

def sync_legacy_catalogue() -> None:
    global _legacy_catalogue_stamp
    if not LEGACY_CATALOG_PATH:
        return
    source = Path(LEGACY_CATALOG_PATH)
    try:
        stat = source.stat()
        stamp = (str(source.resolve()), DB_PATH, stat.st_mtime_ns, stat.st_size)
        if stamp == _legacy_catalogue_stamp:
            return
        from migrate_to_sqlite import import_catalogue
        import_catalogue(source, Path(LEGACY_ASSET_ROOT) if LEGACY_ASSET_ROOT else source.parent)
        _legacy_catalogue_stamp = stamp
    except (OSError, ValueError, TypeError, AttributeError) as error:
        logging.getLogger(__name__).warning("Legacy catalogue import deferred; retaining existing records: %s", error)


def catalogue_firmware_entries(include_new_lines: bool = False, product: str = DEFAULT_OTA_PRODUCT) -> list[dict[str, Any]]:
    sync_legacy_catalogue()
    with get_db_connection() as conn:
        legacy = [dict(row) for row in conn.execute(
            "SELECT id, soc, type, branch, version, url, COALESCE(comment, '') AS comment, release_line, stage, sha256, size, source_sha, rgbwwled_version, upgrade_from_lines FROM firmware_builds"
        )]
        published = conn.execute(
            """SELECT r.release_id, r.version, r.source_commit, r.changelog_json, r.manifest_json,
                      c.channel, a.artifact_id, a.hardware, a.hardware_revision,
                      a.soc, a.build_type, a.sha256, a.size
               FROM ota_releases r JOIN ota_artifacts a USING (release_id)
               JOIN ota_release_channels c USING (release_id)
               JOIN ota_products p ON p.product_id = r.product_id
                             WHERE r.product_id = ? AND p.enabled = 1
                                 AND r.status = 'published' AND a.role = 'firmware' AND a.uploaded = 1""", (product,)
        ).fetchall()
        if product != DEFAULT_OTA_PRODUCT:
                legacy = []
    groups: dict[tuple, list] = {}
    for row in published:
        if not include_new_lines and not row["channel"].startswith("5.0/"):
            continue
        key = (row["version"], row["channel"], row["soc"], row["build_type"])
        groups.setdefault(key, []).append(row)
    entries = [entry for entry in legacy if include_new_lines or parse_version_components(entry["version"])[:2] <= (5, 0)]
    for rows in groups.values():
        if len(rows) != 1:
            continue
        row = rows[0]
        entries.append({
            "soc": row["soc"], "type": row["build_type"], "branch": row["channel"],
            "version": row["version"], "comment": "\n".join(
                entry["subject"] for entry in json.loads(row["changelog_json"])),
            "url": f"{DOWNLOAD_BASE_URL}/api/ota/artifacts/{row['release_id']}/{row['artifact_id']}",
            "id": f"{row['release_id']}:{row['artifact_id']}:{row['channel']}",
            "release_id": row["release_id"], "artifact_id": row["artifact_id"],
            "sha256": row["sha256"], "size": row["size"], "source_sha": row["source_commit"],
            "upgrade_from_lines": json.loads(row["manifest_json"]).get("upgrade_from_lines", []),
        })
    return sorted(entries, key=lambda entry: (version_sort_key(entry["version"]),
                  entry["branch"], entry["soc"], entry["type"], entry["url"]), reverse=True)


def legacy_catalogue_view() -> dict[str, Any]:
    current: dict[tuple, dict] = {}
    history = []
    for entry in catalogue_firmware_entries():
        branch = entry["branch"]
        if entry.get("release_id") and branch.startswith("5.0/"):
            branch = {"develop": "develop", "testing": "testing", "prod": "stable"}[branch.split("/")[1]]
        identity = (entry["soc"], branch, entry["type"])
        projected = {"soc": entry["soc"], "type": entry["type"], "branch": branch,
                     "version": entry["version"], "fw_version": entry["version"],
                     "comment": entry["comment"], "files": {"rom": {"url": entry["url"]}}}
        if identity in current:
            history.append(projected)
        else:
            current[identity] = projected
    with get_db_connection() as conn:
        webapps = [dict(row) for row in conn.execute(
            "SELECT branch, version, type, file_path, files_json, basepath, COALESCE(min_firmware, '') AS min_firmware, COALESCE(comment, '') AS comment FROM webapp_builds ORDER BY branch, version"
        )]
    webapp_current = {}
    webapp_history = []
    for entry in sorted(webapps, key=lambda entry: version_sort_key(entry["version"]), reverse=True):
        try:
            manifest = webapp_manifest(entry, allow_missing=True)
        except HTTPException:
            continue
        entry = {key: value for key, value in entry.items() if key not in {"file_path", "files_json"}} | {
            "files": [{**asset, "path": asset["filename"]} for asset in manifest["files"]], "total_size": manifest["total_size"]}
        if entry["branch"] in webapp_current:
            webapp_history.append(entry)
        else:
            webapp_current[entry["branch"]] = entry
    rom = next((entry["files"]["rom"] | {"fw_version": entry["version"]}
                for entry in current.values() if entry["soc"] == "esp8266" and entry["type"] == "release"), {})
    return {"rom": rom, "spiffs": {}, "firmware": list(current.values()), "history": history,
            "webapp": list(webapp_current.values()), "webapp_history": webapp_history}


@app.get("/version.json", tags=["Legacy Compatibility"])
@app.get("/api/version.json", tags=["Legacy Compatibility"])
def get_legacy_catalogue():
    return legacy_catalogue_view()


def release_identity(version: str, branch: str) -> Optional[dict[str, str]]:
    match = re.match(r"^[Vv]?(\d+)\.(\d+)(?:\.|-|$)", version)
    if not match:
        return None
    line = f"{int(match[1])}.{int(match[2])}"
    suffix = branch.lower().strip()
    qualified = re.fullmatch(r"(\d+\.\d+)[/-](develop|testing|test|prod|stable|experimental)", suffix)
    if qualified:
        if qualified[1] != line:
            return None
        suffix = qualified[2]
    stage = {"develop": "develop", "devel": "develop", "experimental": "develop", "test": "testing", "testing": "testing", "stable": "prod", "prod": "prod"}.get(suffix)
    return {"release_line": line, "stage": stage, "source_ref": f"{line}/{stage}"} if stage else None


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


def firmware_candidates(soc: str, product: str = DEFAULT_OTA_PRODUCT) -> list[dict[str, Any]]:
    product = product.lower()
    _enabled_ota_product(product)
    candidates = []
    for entry in catalogue_firmware_entries(include_new_lines=True, product=product):
        if entry["soc"] != soc.lower() or entry["type"] not in {"debug", "release"}:
            continue
        identity = release_identity(entry["version"], entry["branch"])
        if identity is None or (product == DEFAULT_OTA_PRODUCT and soc.lower() == "esp8266" and identity["release_line"] != "5.0"):
            continue
        if any(entry.get(key) and entry[key] != identity[key] for key in ("release_line", "stage")):
            continue
        identifier = entry["id"] if entry.get("release_id") else f"legacy:{entry['id']}"
        candidates.append({**entry, **identity, "id": identifier, "build_type": entry["type"], "product": product})
    return sorted(candidates, key=lambda entry: (version_sort_key(entry["version"]), str(entry["id"])), reverse=True)


@app.get("/api/v2/firmware/options", tags=["Version-aware Firmware Discovery"])
def firmware_options(soc: str, release_line: Optional[str] = None, build_type: Optional[str] = None, product: str = DEFAULT_OTA_PRODUCT):
    candidates = firmware_candidates(soc, product)
    combinations = {(entry["release_line"], entry["stage"], entry["build_type"]) for entry in candidates
                    if (not release_line or release_line == entry["release_line"])
                    and (not build_type or build_type == entry["build_type"])}
    ordered = sorted(combinations, key=lambda values: (tuple(map(int, values[0].split("."))), values[1:]))
    return {"soc": soc.lower(), "product": product.lower(), "combinations": [dict(zip(("release_line", "stage", "build_type"), values)) for values in ordered]}


@app.get("/api/v2/firmware/versions", tags=["Version-aware Firmware Discovery"])
def firmware_versions_v2(soc: str, release_line: str, stage: str, build_type: str, product: str = DEFAULT_OTA_PRODUCT):
    stage = {"test": "testing", "stable": "prod"}.get(stage, stage)
    if not re.fullmatch(r"\d+\.\d+", release_line) or stage not in {"develop", "testing", "prod"} or build_type not in {"debug", "release"}:
        raise HTTPException(status_code=422, detail="Invalid release line, stage or build type")
    return [entry for entry in firmware_candidates(soc, product) if entry["release_line"] == release_line
            and entry["stage"] == stage and entry["build_type"] == build_type]


@app.get("/api/v2/firmware/latest", tags=["Version-aware Firmware Discovery"])
def firmware_latest_v2(soc: str, release_line: str, stage: str, build_type: str, product: str = DEFAULT_OTA_PRODUCT):
    candidates = firmware_versions_v2(soc, release_line, stage, build_type, product)
    if not candidates:
        raise HTTPException(status_code=404, detail="No firmware matching requested parameters")
    return candidates[0]


@app.get("/api/v2/firmware/transition", tags=["Version-aware Firmware Discovery"])
def firmware_transition(soc: str, installed_version: str, target_id: str, installed_webapp_version: str = "", product: str = DEFAULT_OTA_PRODUCT):
    source = re.match(r"^[Vv]?(\d+\.\d+)(?:\.|-|$)", installed_version)
    target = next((entry for entry in firmware_candidates(soc, product) if str(entry["id"]) == target_id), None)
    if source is None or target is None:
        raise HTTPException(status_code=422, detail="Installed identity or target release is unavailable")
    if source[1] == target["release_line"]:
        return {"status": "ready", "steps": ["install_firmware"]}
    allowed_lines = target.get("upgrade_from_lines") or []
    if isinstance(allowed_lines, str):
        try:
            allowed_lines = json.loads(allowed_lines)
        except ValueError:
            allowed_lines = []
    if not isinstance(allowed_lines, list) or source[1] not in allowed_lines:
        return {"status": "blocked", "reason": "Cross-line configuration/firmware migration has not been qualified", "steps": []}
    bridges = []
    for stage in ("prod", "testing", "develop"):
        current = webapp_versions_v2(installed_version, stage)
        future = {entry["id"] for entry in webapp_versions_v2(target["version"], stage)}
        bridges.extend(entry for entry in current if entry["id"] in future and entry["max_firmware"])
    installed = next((entry for entry in bridges if entry["version"] == installed_webapp_version), None)
    if installed:
        return {"status": "ready", "bridge_version": installed["version"], "steps": ["install_firmware"]}
    if bridges:
        return {"status": "bridge_required", "bridge": bridges[0],
                "reason": f"Install bridge webapp {bridges[0]['version']} before changing firmware line",
                "steps": ["install_bridge_webapp", "verify_bridge_webapp", "install_firmware"]}
    return {"status": "blocked", "reason": "No qualified bridge webapp is available; stay on the installed line", "steps": []}


def canonical_stage(value: str) -> str:
    suffix = value.lower().split("/")[-1]
    qualified = re.fullmatch(r"\d+\.\d+-(develop|testing|test|stable|prod|experimental)", suffix)
    if qualified:
        suffix = qualified[1]
    return {"develop": "develop", "devel": "develop", "experimental": "develop", "testing": "testing", "test": "testing", "stable": "prod", "prod": "prod"}.get(suffix, "")


def webapp_manifest(record: dict, allow_missing: bool = False) -> dict:
    directory = Path(record["file_path"])
    declared = json.loads(record.get("files_json") or "[]")
    if not directory.is_dir() and not (allow_missing and declared):
        raise HTTPException(status_code=404, detail="Webapp files are unavailable")
    files = []
    for asset in declared:
        name = asset.get("filename") or asset.get("path")
        if not name or not (directory / name).resolve().is_relative_to(directory.resolve()):
            raise HTTPException(status_code=409, detail="Invalid legacy webapp asset path")
        path = directory / name
        if not path.is_file() and not allow_missing:
            raise HTTPException(status_code=404, detail="Legacy webapp asset is missing")
        files.append({"filename": name, "md5": asset["md5"],
                      "size": path.stat().st_size if path.is_file() else asset.get("size", 0)})
    if files:
        return {"version": record["version"], "branch": record["branch"], "basepath": record["basepath"],
                "files": files, "total_size": sum(asset["size"] for asset in files)}
    for asset in sorted(directory.rglob("*")):
        if asset.is_symlink():
            raise HTTPException(status_code=409, detail="Webapp contains unsupported links")
        if asset.is_file():
            payload = asset.read_bytes()
            files.append({"filename": asset.relative_to(directory).as_posix(),
                          "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(), "size": len(payload)})
    if not files:
        raise HTTPException(status_code=404, detail="Webapp has no installable files")
    return {"version": record["version"], "branch": record["branch"], "basepath": record["basepath"],
            "files": files, "total_size": sum(asset["size"] for asset in files)}


@app.get("/api/v2/webapp/versions", tags=["Version-aware Webapp Discovery"])
def webapp_versions_v2(firmware_version: str, stage: str, asset_budget: Optional[int] = None):
    sync_legacy_catalogue()
    requested_stage = canonical_stage(stage)
    firmware_line = re.match(r"^[Vv]?(\d+\.\d+)(?:\.|-|$)", firmware_version)
    if not requested_stage or firmware_line is None:
        raise HTTPException(status_code=422, detail="Firmware identity and stage are required")
    with get_db_connection() as connection:
        records = [dict(row) for row in connection.execute("SELECT * FROM webapp_builds")]
    result = []
    for record in records:
        if canonical_stage(record["branch"]) != requested_stage:
            continue
        minimum, maximum = record["min_firmware"] or "", record["max_firmware"] or ""
        if minimum and parse_version_components(firmware_version) < parse_version_components(minimum):
            continue
        if maximum and parse_version_components(firmware_version) >= parse_version_components(maximum):
            continue
        if not maximum:
            legacy_line = re.match(r"^[Vv]?(\d+\.\d+)(?:\.|-|$)", minimum or record["version"])
            if legacy_line is None or legacy_line[1] != firmware_line[1]:
                continue
        if record["api_min"] or record["api_max"]:
            continue
        try:
            required = json.loads(record["required_capabilities"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(required, list) or not all(isinstance(value, str) for value in required):
            continue
        if required:
            continue
        try:
            manifest = webapp_manifest(record)
        except HTTPException:
            continue
        if asset_budget is not None and manifest["total_size"] > asset_budget:
            continue
        result.append({**manifest, "id": str(record["id"]), "stage": requested_stage,
                       "min_firmware": minimum, "max_firmware": maximum})
    return sorted(result, key=lambda record: version_sort_key(record["version"]), reverse=True)


@app.get("/api/v2/webapp/options", tags=["Version-aware Webapp Discovery"])
def webapp_options_v2(firmware_version: str, asset_budget: Optional[int] = None):
    return {"stages": [stage for stage in ("develop", "testing", "prod")
                       if webapp_versions_v2(firmware_version, stage, asset_budget)]}


@app.get("/api/v2/webapp/latest", tags=["Version-aware Webapp Discovery"])
def webapp_latest_v2(firmware_version: str, stage: str, asset_budget: Optional[int] = None):
    candidates = webapp_versions_v2(firmware_version, stage, asset_budget)
    if not candidates:
        raise HTTPException(status_code=404, detail="No compatible webapp; existing installation remains valid")
    return candidates[0]


@app.get("/api/webapp/latest", tags=["Webapp Discovery"])
def get_latest_webapp(branch: str, firmware_version: str):
    stage = canonical_stage(branch)
    if not stage and re.fullmatch(r"\d+\.\d+", branch):
        stage = canonical_stage(re.sub(r"^[Vv]?\d+\.\d+(?:\.\d+)?-\d+-", "", firmware_version))
    candidates = webapp_versions_v2(firmware_version, stage)
    candidate = next((entry for entry in candidates if entry["branch"] == branch or entry["max_firmware"]
                      or entry["branch"] == f"{parse_version_components(firmware_version)[0]}.{parse_version_components(firmware_version)[1]}/{stage}"), None)
    if candidate is None:
        raise HTTPException(status_code=404, detail="No compatible webapp in the requested legacy feed")
    return {key: candidate[key] for key in ("version", "branch", "basepath", "files", "total_size")}


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
    max_firmware: str = Form(""),
    api_min: str = Form(""),
    api_max: str = Form(""),
    required_capabilities: str = Form("[]"),
):
    if not all(_safe_segment(value) for value in branch.split("/")) or not _safe_segment(version):
        raise HTTPException(status_code=422, detail="Invalid webapp release path")
    try:
        required = json.loads(required_capabilities)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid capability requirements") from exc
    if not isinstance(required, list) or not all(isinstance(value, str) for value in required):
        raise HTTPException(status_code=422, detail="Invalid capability requirements")
    if max_firmware and (not min_firmware or parse_version_components(max_firmware) <= parse_version_components(min_firmware)):
        raise HTTPException(status_code=422, detail="Invalid firmware compatibility range")
    payload = await archive.read()
    verify_sig(payload, signature)
    checksum = hashlib.sha256(payload).hexdigest()
    branch = branch.lower()
    target_dir = STATIC_ROOT / "webapps" / branch / version
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    basepath = f"{DOWNLOAD_BASE_URL}/downloads/webapps/{branch}/{version}/"
    with tempfile.TemporaryDirectory(dir=target_dir.parent) as temporary:
        staging = Path(temporary) / "assets"
        staging.mkdir()
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as bundle:
                if any(not (entry.isfile() or entry.isdir()) for entry in bundle.getmembers()):
                    raise HTTPException(status_code=422, detail="Archive links and special files are not allowed")
                bundle.extractall(path=staging, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise HTTPException(status_code=400, detail="Invalid webapp archive") from exc
        if not any((staging / name).is_file() for name in ("index.html", "index.html.gz")):
            raise HTTPException(status_code=422, detail="Webapp has no root entry point")
        with get_db_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM webapp_builds WHERE branch = ? AND version = ?", (branch, version)).fetchone()
            if existing:
                unchanged = existing["sha256"] == checksum and all((existing[key] or "") == value for key, value in
                    (("min_firmware", min_firmware), ("max_firmware", max_firmware), ("api_min", api_min), ("api_max", api_max)))
                if not unchanged or json.loads(existing["required_capabilities"] or "[]") != required:
                    raise HTTPException(status_code=409, detail="Webapp release is immutable; publish a new version")
                return {"status": "success", "basepath": existing["basepath"]}
            if target_dir.exists():
                raise HTTPException(status_code=409, detail="Webapp directory already exists")
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
    return sorted({entry.soc for entry in get_firmware_versions(branch=branch, type=type)})


@app.get("/api/firmware/branches", response_model=List[str], tags=["Firmware Discovery"])
def get_firmware_branches(soc: Optional[str] = None, type: Optional[str] = None):
    return sorted({entry.branch for entry in get_firmware_versions(soc=soc, type=type)})


@app.get("/api/firmware/types", response_model=List[str], tags=["Firmware Discovery"])
def get_firmware_types(soc: Optional[str] = None, branch: Optional[str] = None):
    return sorted({entry.type for entry in get_firmware_versions(soc=soc, branch=branch)})


@app.get("/api/firmware/versions", response_model=List[FirmwareEntry], tags=["Firmware Discovery"])
def get_firmware_versions(soc: Optional[str] = None, branch: Optional[str] = None, type: Optional[str] = None):
    qualified = bool(branch and re.fullmatch(r"\d+\.\d+/(develop|testing|prod)", branch))
    entries = catalogue_firmware_entries(include_new_lines=qualified)
    result = []
    for entry in entries:
        if soc and entry["soc"] != soc.lower():
            continue
        if type and entry["type"] != type.lower():
            continue
        if entry.get("release_id") and not qualified and entry["branch"].startswith("5.0/"):
            entry = {**entry, "branch": {"develop": "develop", "testing": "testing", "prod": "stable"}[entry["branch"].split("/")[1]]}
        if branch and entry["branch"] != branch.lower():
            continue
        result.append(FirmwareEntry(**entry))
    return result


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
    sync_legacy_catalogue()
    with get_db_connection() as conn:
        rows = conn.execute("SELECT DISTINCT branch FROM webapp_builds").fetchall()
    return sorted([row["branch"] for row in rows])


@app.get("/api/webapp/versions", response_model=List[WebappEntry], tags=["Webapp Discovery"])
def get_webapp_versions(branch: Optional[str] = None, firmware_version: Optional[str] = None):
    sync_legacy_catalogue()
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


app.router.routes[:] = [route for route in app.router.routes
                       if route.path not in {"/api/ota/latest", "/api/ota/artifacts/{release_id}/{artifact_id}"} and
                       (not getattr(route, "methods", None) or route.methods <= {"GET", "HEAD"})]
app.openapi_schema = None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("version_api:app", host=LISTEN_HOST, port=LISTEN_PORT, reload=False)

