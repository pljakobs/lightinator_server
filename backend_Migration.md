# Lightinator Build & Release Distribution Redesign

This plan documents the architectural decisions, API contracts, security measures, and migration path required to modernize the Lightinator build distribution pipeline.

---

## 1. System Architecture Decisions

* **Database Engine:** Transition from static `version.json` management to SQLite (`firmware.db`). SQLite provides native ACID transactions to prevent file-locking race conditions during concurrent CI uploads while acting as the single source of truth.
* **Backward Compatibility:** Maintain legacy JSON API responses by serving dynamically generated views or fallback endpoints so existing device clients remain unaffected.
* **Deployment Workflow:** CI runners build binary/static assets, compress multi-file bundles, sign payloads locally, and push artifacts via an authenticated REST API.
* **Storage Structure:** Artifacts are extracted or written into structured static filesystem paths under the web server root (`/var/www/downloads/firmware/...` and `/var/www/downloads/webapps/...`).

---

## 2. Authentication & Cryptographic Signing

### Authentication Token
* **Transport:** Passed in the HTTP `Authorization` header as a Bearer token (`Authorization: Bearer <TOKEN>`).
* **CI Config:** Stored as a GitHub Secret (`LIGHTINATOR_CI_UPLOAD_TOKEN`).
* **Verification:** Validated by FastAPI before processing request metadata or accepting files.

### Artifact Signing & Verification
* **Algorithm:** Ed25519 asymmetric signing.
* **Key Management:**
  * **Private Key:** Stored as a GitHub Secret (`LIGHTINATOR_CI_SIGNING_KEY`). Never leaves the CI runner environment.
  * **Public Key:** Configured on the API server (`RELEASE_PUBLIC_KEY_HEX`).
* **Process:**
  1. The CI runner generates a detached hex signature over the raw binary payload or archive file.
  2. The signature is sent in the `signature` form field during upload.
  3. The API server verifies the signature against the payload bytes **in memory prior to any disk writes or archive extraction**.

---

## 3. Artifact Bundling Strategy

* **Firmware Targets (`.bin`):** Uploaded as single uncompressed binary files.
* **Webapp Packages (`.tar.gz`):** 
  * All static assets (HTML, JS, CSS, assets) are bundled into a gzipped tarball during the CI build stage (`tar -czf webapp_build.tar.gz -C dist/ .`).
  * The API server extracts the archive upon receipt.
* **Extraction Safety:** The server enforces path sanitization (Python 3.12+ `filter='data'`) to prevent directory traversal vulnerabilities (Zip Slip).

---

## 4. SQLite Database Schema

```sql
-- Firmware release tracking
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

-- Webapp release tracking
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
```

---

## 5. API Specification

### 5.1 Upload Firmware Build
* **Endpoint:** `POST /api/firmware/upload`
* **Headers:** `Authorization: Bearer <CI_TOKEN>`
* **Content-Type:** `multipart/form-data`

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `soc` | `string` | Target SoC architecture (`esp8266`, `esp32`, `esp32c3`) |
| `type` | `string` | Build type (`release`, `debug`, `bugfix`) |
| `branch` | `string` | Git branch (`main`, `testing`, `develop`) |
| `version` | `string` | Semantic git version tag |
| `signature` | `string` | Hex-encoded Ed25519 signature of the `.bin` file |
| `comment` | `string` | Optional release comment |
| `file` | `file` | Single firmware binary file (`.bin`) |

### 5.2 Upload Webapp Build
* **Endpoint:** `POST /api/webapp/upload`
* **Headers:** `Authorization: Bearer <CI_TOKEN>`
* **Content-Type:** `multipart/form-data`

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `branch` | `string` | Git branch (`main`, `testing`, `develop`) |
| `version` | `string` | Webapp semver tag |
| `type` | `string` | Build type (`release`, `debug`) |
| `min_firmware` | `string` | Minimum compatible controller firmware version |
| `signature` | `string` | Hex-encoded Ed25519 signature of the `.tar.gz` archive |
| `comment` | `string` | Optional release comment |
| `archive` | `file` | Gzipped webapp tarball (`.tar.gz`) |

---

## 6. Implementation Reference

### FastAPI Upload & Verification (`version_api.py`)

```python
import os
import sqlite3
import tarfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, Form, UploadFile, File, Depends
from cryptography.hazmat.primitives.asymmetric import ed25519

app = FastAPI()

DB_PATH = os.getenv("VERSION_DB_PATH", "firmware.db")
STATIC_ROOT = Path(os.getenv("VERSION_HTML_ROOT", "/var/www/downloads"))
CI_TOKEN = os.getenv("CI_UPLOAD_TOKEN", "")
PUBLIC_KEY_HEX = os.getenv("RELEASE_PUBLIC_KEY_HEX", "")

PUBLIC_KEY = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX))

def verify_token(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "")
    if not CI_TOKEN or token != CI_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized token")

def verify_sig(data: bytes, signature_hex: str):
    try:
        PUBLIC_KEY.verify(bytes.fromhex(signature_hex), data)
    except Exception:
        raise HTTPException(status_code=400, detail="Cryptographic signature verification failed")

@app.post("/api/webapp/upload", status_code=201)
async def upload_webapp(
    branch: str = Form(...),
    version: str = Form(...),
    type: str = Form("debug"),
    min_firmware: str = Form(""),
    signature: str = Form(...),
    comment: str = Form(""),
    archive: UploadFile = File(...),
    _: None = Depends(verify_token)
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
        raise HTTPException(status_code=400, detail="Invalid tar archive")

    basepath = f"/downloads/webapps/{branch}/{version}/"
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO webapp_builds 
            (branch, version, type, min_firmware, file_path, basepath, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (branch, version, type, min_firmware, str(target_dir), basepath, comment)
        )
        conn.commit()

    return {"status": "success", "basepath": basepath}
```

---

## 7. Operational Workflow & Execution

### Required GitHub Actions Secrets
* `LIGHTINATOR_CI_UPLOAD_TOKEN`: API access token.
* `LIGHTINATOR_CI_SIGNING_KEY`: Ed25519 private key in hex format.

### Example GitHub Actions Step

```yaml
- name: Package, Sign, and Deploy Webapp
  env:
    UPLOAD_TOKEN: ${{ secrets.LIGHTINATOR_CI_UPLOAD_TOKEN }}
    PRIVATE_KEY: ${{ secrets.LIGHTINATOR_CI_SIGNING_KEY }}
  run: |
    # 1. Package artifacts
    tar -czf webapp.tar.gz -C dist/ .

    # 2. Generate signature
    SIG=$(python3 -c "
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex('$PRIVATE_KEY'))
    with open('webapp.tar.gz', 'rb') as f:
        print(key.sign(f.read()).hex())
    ")

    # 3. Publish to server API
    curl -X POST "[https://lightinator.de/api/webapp/upload](https://lightinator.de/api/webapp/upload)" \
      -H "Authorization: Bearer ${UPLOAD_TOKEN}" \
      -F "branch=${GITHUB_REF_NAME}" \
      -F "version=${GIT_TAG_OR_SHA}" \
      -F "type=release" \
      -F "signature=${SIG}" \
      -F "archive=@webapp.tar.gz"
```
