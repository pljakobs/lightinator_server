
<img align="left" width="96" height="96" src="images/lightinator-distribution-server-icon-dark.svg" alt="Lightinator Distribution Server icon">

# lightinator-server


Server-side configuration and tooling for [lightinator.de](https://lightinator.de).

## Contents

| Path | Description |
|------|-------------|
| `manage_version.py` | CLI tool to manage `version.json` (canonical home — firmware repo carries a copy) |
| `version_api.py` | Read-only HTTP API over `version.json`, listens on `0.0.0.0:8765` (host); nginx container proxies `/api/` via `host.containers.internal:8765` |
| `containers/nginx.container` | Podman Quadlet unit for the nginx reverse proxy |
| `nginx/default.conf` | nginx site config — deployed to `/home/github-deploy/nginx/conf.d/` |
| `fail2ban/filter.d/` | Custom fail2ban filters (gh-knocker, nginx-podlet-access) |
| `fail2ban/action.d/` | Custom fail2ban action (f2b-whitelist — grants SSH access on knock) |
| `fail2ban/jail.d/` | Jail config stanza for all custom jails |
| `systemd/version-api.service` | systemd unit for `version_api.py` |
| `deploy/setup.sh` | Idempotent first-run bootstrap (run as root) |
| `deploy/update.sh` | Pull repo + reload changed services (run as root, called by CI) |

## Server layout

```
/home/github-deploy/
  lightinator-server/     ← this repo
  nginx/
    conf.d/default.conf   ← symlink-managed by update.sh
    html/                 ← static files + version.json (deployed by CI)
    logs/
/etc/containers/systemd/nginx.container
/etc/systemd/system/version-api.service
/etc/fail2ban/filter.d/gh-knocker.conf          (hash substituted from secret)
/etc/fail2ban/filter.d/nginx-podlet-access.conf
/etc/fail2ban/action.d/f2b-whitelist.conf
/etc/fail2ban/jail.d/lightinator.conf
```

## Secrets and keys

### Port-knock secret (`DEPLOY_KNOCK_HASH`)

The gh-knocker mechanism uses a secret URL path to whitelist a CI IP for SSH access. The path contains a high-entropy hex hash that must never appear in version-controlled files.

**Generating the hash**

```bash
openssl rand -hex 32
```

**Where it lives**

| Location | Form | Purpose |
|----------|------|---------|
| `knock-secret.env` in the repo root (gitignored) | `DEPLOY_KNOCK_HASH=<hash>` | Read automatically by `setup.sh` and `update.sh`; never committed |
| GitHub Actions secret `DEPLOY_KNOCK_URL` in `esp_rgbww_firmware` | full URL `https://lightinator.de/deploy-knock-<hash>` | Used by the firmware CI deploy workflow to knock before SSH |
| GitHub Actions secret `DEPLOY_KNOCK_URL` in `esp_rgb_webapp2` | full URL `https://lightinator.de/deploy-knock-<hash>` | Used by the webapp CI deploy workflow to knock before SSH |

The hash is the same value in all three locations. If it is ever rotated, update all three together then re-run `setup.sh` or `update.sh` with the new `DEPLOY_KNOCK_HASH`.

**How it works**

1. CI `curl`s the knock URL (a 404 is fine — nginx logs the request regardless).
2. fail2ban's `gh-knocker` jail matches the request and adds the runner IP to the sshd jail's ignore list for 10 minutes.
3. CI connects via SSH within that window.
4. fail2ban's `gh-knocker` jail unregisters the IP after `bantime` expires.

---

### Deploy SSH key — firmware CI (`DEPLOY_KEY` in `esp_rgbww_firmware`)

This key authorises the firmware build+deploy workflow to rsync firmware binaries and `version.json` updates to the server.

**Setup (one-time)**

```bash
# On your local machine — generate a dedicated key pair, no passphrase:
ssh-keygen -t ed25519 -f ~/.ssh/lightinator_firmware_deploy -C "github-actions/esp_rgbww_firmware" -N ""

# Add the public key to the server:
ssh lightinator.de "cat >> /home/github-deploy/.ssh/authorized_keys" \
    < ~/.ssh/lightinator_firmware_deploy.pub

# Add the private key as a GitHub Actions secret in esp_rgbww_firmware:
#   Settings → Secrets and variables → Actions → New repository secret
#   Name: DEPLOY_KEY
#   Value: contents of ~/.ssh/lightinator_firmware_deploy
```

**What the workflow uses it for**

- `webfactory/ssh-agent` loads the key at the start of the publish job.
- rsync over SSH: pushes firmware `.bin` files to `/home/github-deploy/nginx/html/download/`.
- SSH command: runs `manage_version.py` remotely to update `version.json`.

---

### Deploy SSH key — webapp CI (`DEPLOY_KEY` in `esp_rgb_webapp2`)

Authorises the webapp build+deploy workflow to rsync the compiled SPA to the server.

**Setup (one-time)**

```bash
# Generate a separate key pair for the webapp repo:
ssh-keygen -t ed25519 -f ~/.ssh/lightinator_webapp_deploy -C "github-actions/esp_rgb_webapp2" -N ""

# Add public key to the server (separate line from the firmware key):
ssh lightinator.de "cat >> /home/github-deploy/.ssh/authorized_keys" \
    < ~/.ssh/lightinator_webapp_deploy.pub

# Add private key as a GitHub Actions secret in esp_rgb_webapp2:
#   Name: DEPLOY_KEY
#   Value: contents of ~/.ssh/lightinator_webapp_deploy
```

**What the workflow uses it for**

- rsync over SSH: pushes gzipped webapp assets to `/home/github-deploy/nginx/html/`.
- SSH command: runs `manage_version.py add-webapp` remotely to update `version.json`.

---

### Server environment variable summary

| Variable | Required by | Notes |
|----------|-------------|-------|
| `DEPLOY_KNOCK_HASH` | `setup.sh`, `update.sh` | 64-char hex; never committed |
| `CERTBOT_EMAIL` | `setup.sh` | Optional; used for Let's Encrypt account registration |
| `DEPLOY_USER` | `setup.sh`, `update.sh` | Defaults to `github-deploy` |
| `DOMAIN` | `setup.sh` | Defaults to `lightinator.de` |
| `CI_UPLOAD_TOKEN` | `version-api.env` | Bearer credential for legacy and product-aware CI uploads |
| `OTA_ADMIN_TOKEN` | `version-api.env` | Separate bearer credential for registering products |
| `OTA_PROMOTION_TOKEN` | `version-api.env` | Separate bearer credential for promoting published releases |
| `RELEASE_PUBLIC_KEY_HEX` | `version-api.env` | 32-byte Ed25519 public key used to seed the controller product on first DB initialization |
| `OTA_STORAGE_ROOT` | `version-api.env` | Private staging/published artifact storage; defaults to `/var/lib/lightinator/ota-artifacts` |
| `VERSION_DB_PATH` | `version-api.env` | SQLite catalogue path; defaults to `firmware.db` in the server working directory |

## Initial setup

Clone the repo to the server and run the bootstrap script as root:file:///

```bash
git clone https://github.com/pljakobs/lightinator_server.git \
    /home/github-deploy/lightinator-server

# Create the gitignored secrets file:
echo 'DEPLOY_KNOCK_HASH=<64-char hex from server secret>' \
    > /home/github-deploy/lightinator-server/knock-secret.env
chmod 600 /home/github-deploy/lightinator-server/knock-secret.env

export CERTBOT_EMAIL=admin@lightinator.de   # optional
bash /home/github-deploy/lightinator-server/deploy/setup.sh
```

`setup.sh` is fully idempotent — it installs packages (`podman`, `fail2ban`, `certbot`, `python3`, `firewalld`), creates the `github-deploy` user, opens firewall ports 80/443, creates the Podman `monitoring-stack` network, issues the TLS certificate, deploys all config files, and enables all services.

After setup, add deploy public keys to `/home/github-deploy/.ssh/authorized_keys`.

## Updating

```bash
# Called automatically by CI after rsync deploy, or manually:
export DEPLOY_KNOCK_HASH=<hash>   # only needed if fail2ban filters changed
sudo bash /home/github-deploy/lightinator-server/deploy/update.sh
```

## Version API

`version_api.py` serves legacy discovery APIs from SQLite and exposes a separate
product-aware OTA catalog, also stored in SQLite. nginx proxies `/api/` to
`http://host.containers.internal:8765/api/`.

The API is served by FastAPI/Uvicorn and publishes generated documentation:

```
https://lightinator.de/api/docs
https://lightinator.de/api/redoc
https://lightinator.de/api/openapi.json
```

### Product-Aware OTA API

The OTA catalog is additive; the existing `/api/firmware/*` and
`/api/webapp/*` routes and SSH-based publisher remain unchanged. SQLite is the
authority for the new catalog. These endpoints do not write `version.json`; a
legacy projection can be generated from SQLite separately when needed.

Only `controller` is registered by default. Its target policy allows ESP8266,
ESP32 and ESP32-C3 for 5.0, and ESP32/ESP32-C3 for 5.1. New product IDs such as
`wallpanel` or `color_sensor` must be explicitly registered by an administrator
with a dedicated Ed25519 public key, source repository and version-line/SoC
policy. No other product is currently enabled.

CI creates a signed release manifest containing product, version, channel,
source repository/commit, required changelog commit/subject entries, hardware
targets and artifact roles, filenames, sizes and SHA-256 hashes. The Ed25519
signature covers canonical JSON: sorted keys, compact separators and UTF-8
encoding of the manifest with defaults included. The changelog is therefore
covered by the same signature as the artifact metadata.

Discovery responses include the canonical manifest, its SHA-256 digest, the
Ed25519 signature, and the release public-key snapshot. OTA artifact URLs use
HTTP by default to support ESP8266 clients. A device must compare the returned
key with a public key pinned in firmware, verify the manifest signature and
digest, and verify each downloaded artifact against the signed manifest before
installation. A key delivered by this API over HTTP is not itself a trust
anchor.

Release flow:

1. `POST /api/ota/releases` authenticates the CI bearer token, validates the
    manifest against the registered product policy and verifies its signature.
    It creates a staged release and returns one upload URL per artifact.
2. `PUT /api/ota/releases/{release_id}/artifacts/{artifact_id}` streams raw
    artifact bytes into private staging storage and verifies the declared size
    and SHA-256. Individual artifacts are limited to 64 MiB and a release to
    512 MiB.
3. `POST /api/ota/releases/{release_id}/finalize` requires every declared
    artifact, re-verifies hashes, atomically publishes the complete release and
    adds it to its origin channel. Staged files are removed after publication.
4. `POST /api/ota/releases/{release_id}/promote` uses the separate promotion
    bearer token. Promotion is restricted to channels on the release's version
    line; CI cannot publish directly to `prod`.
5. `GET /api/ota/latest?product=controller&channel=5.0/develop&hardware=...&soc=esp8266&build_type=release`
    returns the newest published matching target, including changelog and
    artifact URLs. No compatible release returns 404.

The product registry endpoint is `GET /api/ota/products`; registering a product
uses `POST /api/ota/products` with the admin bearer token. Configure the
credentials in `/etc/lightinator/version-api.env`, readable by the service user
and not served by nginx. Required secrets are `CI_UPLOAD_TOKEN`,
`OTA_ADMIN_TOKEN`, `OTA_PROMOTION_TOKEN`, and `RELEASE_PUBLIC_KEY_HEX` for the
initial controller key. Set a dedicated `OTA_STORAGE_ROOT` if the default
`/var/lib/lightinator/ota-artifacts` is unsuitable. The setup and update scripts
create the private storage directory and an empty optional environment file;
operators must provision the secrets before enabling API publishing.

The server API implementation is not yet wired into firmware CI. Until that
migration is completed, the existing SSH/rsync publisher and legacy catalogue
remain the production path.

### Concepts

The firmware catalogue is an **n-cube** with three axes. Any combination of
axis filters can be applied; omitting an axis returns all values on that axis.

| Axis | Values (example) |
|------|-----------------|
| `soc` | `esp8266`, `esp32`, `esp32c3` |
| `branch` | `testing`, `develop`, `experimental`, `crash-reporting` |
| `type` | `release`, `debug` |

The webapp catalogue has one axis: `branch`.

Version strings follow the pattern `V<major>.<minor>-<build>[-<suffix>]`
(e.g. `V5.0-599-testing`). The trailing `-<suffix>` is ignored during
comparison; only `<major>.<minor>-<build>` is used for ordering. The raw
device firmware version string can be passed directly to `firmware_version=`.

### Discovery endpoints

```
GET /api/firmware/socs[?branch=<branch>][&type=<type>]
    → ["esp32", "esp32c3", "esp8266"]

GET /api/firmware/branches[?soc=<soc>][&type=<type>]
    → ["crash-reporting", "develop", "experimental", "testing"]

GET /api/firmware/types[?soc=<soc>][&branch=<branch>]
    → ["debug", "release"]

GET /api/webapp/branches
    → ["experimental"]
```

### Firmware version queries

All three filter parameters are optional. Omit any axis to slice across all its values.

```
GET /api/firmware/versions[?soc=<soc>][&branch=<branch>][&type=<type>]
```
Returns all matching firmware entries sorted newest-first.

```
GET /api/firmware/latest[?soc=<soc>][&branch=<branch>][&type=<type>]
```
When all three axes are specified, returns a single object.
When one or more axes are omitted, returns an array — one entry per unique
`(soc, branch, type)` combination, each being the latest in its group.

**Firmware entry object:**
```json
{
  "version": "V5.0-599-testing",
  "soc":     "esp32",
  "branch":  "testing",
  "type":    "release",
  "url":     "https://lightinator.de/download/testing/V5.0-599-testing/esp32/release/app.bin",
  "comment": ""
}
```

### Webapp version queries

```
GET /api/webapp/versions[?branch=<branch>][&firmware_version=<ver>]
GET /api/webapp/latest[?branch=<branch>][&firmware_version=<ver>]
```
`firmware_version` filters out webapp entries whose `min_firmware` requirement
is not satisfied. When `branch` is omitted, `/latest` returns one entry per branch.

**Webapp entry object:**
```json
{
  "version":      "V5.0-316-experimental",
  "branch":       "experimental",
  "type":         "release",
  "min_firmware": "",
  "files":        [],
  "comment":      ""
}
```

### Example workflow

```bash
# What SOCs are available?
curl https://lightinator.de/api/firmware/socs

# What branches exist for esp32?
curl "https://lightinator.de/api/firmware/branches?soc=esp32"

# All debug builds for esp32 (across all branches), newest first:
curl "https://lightinator.de/api/firmware/versions?soc=esp32&type=debug"

# Latest release firmware for esp32, testing branch:
curl "https://lightinator.de/api/firmware/latest?soc=esp32&branch=testing&type=release"

# Latest of every (soc, branch, type) combination — full overview:
curl https://lightinator.de/api/firmware/latest

# Latest webapp compatible with a specific firmware version:
curl "https://lightinator.de/api/webapp/latest?branch=experimental&firmware_version=V5.0-599-testing"
```

## Port knocking

SSH access from GitHub Actions is gated by a fail2ban port-knock mechanism.
See [Secrets and keys → Port-knock secret](#port-knock-secret-deploy_knock_hash) above for the full description, rotation procedure, and where the secret is stored.
