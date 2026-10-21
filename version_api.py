#!/usr/bin/env python3
"""
Lightinator Release & Distribution Service
Provides secure upload endpoints for CI deployment and read-only version discovery for clients.
"""

from __future__ import annotations

import os
import hashlib
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
from fastapi.responses import FileResponse, HTMLResponse
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
    target_policy = json.loads(product["target_policy_json"])
    allowed_socs = {soc.lower() for soc in target_policy.get(version_line, [])}
    if not allowed_socs:
        raise HTTPException(status_code=422, detail="Product is not enabled for this version line")
    unsupported = sorted({target.soc.lower() for target in manifest.targets} - allowed_socs)
    if unsupported:
        raise HTTPException(status_code=422, detail=f"Unsupported SoC for product/version line: {', '.join(unsupported)}")


@app.get("/api/ota/products", tags=["OTA Release Catalog"])
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

