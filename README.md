
<img align="left" width="96" height="96" src="images/lightinator-distribution-server-icon-dark.svg" alt="Lightinator Distribution Server icon">

# lightinator-server

## Public WebUI demo

The installer links to `/api/demo/`, which resolves the newest published
`5.1/develop` build and serves its compressed package assets under a
versioned path. The WebUI supplies the browser-local simulation, with no
server-side controller state shared between visitors. An incompatible or
incomplete latest build produces an explicit error rather than falling back.

The version API reads packages from
`/home/github-deploy/nginx/html/download/webui/5.1/develop` by default; set
`VERSION_DEMO_ROOT` to change this path. Both deployment scripts install the
landing page after verifying the API can serve the latest demo. Publish a
demo-enabled WebUI before deploying. Only `html/index.html` is copied into the
webroot; existing images, flashing manifests and downloads are preserved.


Server-side configuration and tooling for [lightinator.de](https://lightinator.de).

## Active Publishing Path

Publishing continues through the secured SSH/rsync process, not HTTP uploads.
The server-side `manage_version.py` now writes SQLite directly. Its first
`version.json` argument is retained for CLI compatibility but is never read or
written. The public JSON endpoint is a database projection.

Deploy the compatible server/database schema before enabling the updated
firmware/webapp workflows. They retain per-run publisher uploads, but source them
from this server repository, not a firmware checkout. In both publishing repositories,
set the Actions repository variable `DISTRIBUTION_PUBLISHER_SHA` to a reviewed,
published, lowercase 40-character commit SHA containing the updated publisher and
tests. There is deliberately no moving-branch or default-pin fallback.

CI checks out and tests exactly that revision, then uploads a run-specific bundle
containing the renamed publisher, its API/importer modules, and required brand
assets. Firmware and webapp bundles use distinct prefixes. The remote script uses
the server virtualenv and explicit database/webroot paths, checks the source pin and
requires `--publisher-contract` to return `lightinator-sqlite-v1`, then removes only
its own bundle. It does not replace or delete the installed server code.

The following is an equivalent manual server-side invocation; CI executes its
uploaded run-specific copy instead:

```bash
export VERSION_DB_PATH=/home/github-deploy/lightinator-server/firmware.db
export VERSION_ASSET_ROOT=/home/github-deploy/nginx/html
PYTHON=/home/github-deploy/lightinator-server/.venv/bin/python
SCRIPT=/home/github-deploy/lightinator-server/manage_version.py
"$PYTHON" "$SCRIPT" --publisher-contract
"$PYTHON" "$SCRIPT" version.json add esp32 release 5.1/develop V5.1.0 \
    https://lightinator.de/download/firmware/5.1/develop/V5.1.0/esp32/release/app.bin
```

The database must already exist; an explicit `--init-db` before the subcommand is
available for an intentionally new test database. Asset URLs resolve under the
configured webroot and files must already exist there after rsync. Firmware
checksums and webapp manifest hashes are computed from those assets. Changed
content under an existing verified release identity is rejected.

Webapp publishing accepts the existing `--files-json-b64`, `--type`, and
`--min-firmware` options, plus `--max-firmware` for qualified bridge bundles.
Explicit API-contract management flags are deferred. Omitted compatibility
bounds remain unchanged on retries. Firmware accepts optional `--source-sha`,
`--rgbwwled-version`, and `--upgrade-from-lines`; qualification is not inferred.

`cull` currently retains all releases/assets to avoid deleting an upgrade bridge.
`delete` removes selected legacy database records but deliberately retains files;
product-aware signed releases are not deleted through this command. HTTP
publishing and release lifecycle routes are not registered by this discovery app.
Enabling them later requires a separate application and security review.
No production publication is performed by the test suite.

## Contents

| Path | Description |
|------|-------------|
| `manage_version.py` | Canonical SQLite publisher invoked through secured SSH after rsync; JSON is not writable storage. |
| `version_api.py` | Database-backed discovery and legacy JSON views, listens on `0.0.0.0:8765`; HTTP publishing is disabled by default. |
| `containers/nginx.container` | Podman Quadlet unit for the nginx reverse proxy |
| `nginx/default.conf` | nginx site config — deployed to `/home/github-deploy/nginx/conf.d/` |
| `fail2ban/filter.d/` | Custom fail2ban filters (gh-knocker, nginx-podlet-access) |
| `fail2ban/action.d/` | Custom fail2ban action (f2b-whitelist — grants SSH access on knock) |
| `fail2ban/jail.d/` | Jail config stanza for all custom jails |
| `systemd/version-api.service` | systemd unit for `version_api.py` |
| `deploy/setup.sh` | Idempotent first-run bootstrap (run as root) |
| `deploy/update.sh` | Deploy the selected clean checkout and verify the demo (run as root) |
| `deploy/common.sh` | Shared application deployment and managed-file recovery |
| `deploy/recovery.py` | Mandatory private pre-deployment snapshots and standalone restore |

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
| `/etc/lightinator/knock-secret.env` (root-owned, mode 600) | `DEPLOY_KNOCK_HASH=<hash>` | Read only by `setup.sh`; never copied into the checkout, webroot, logs or deployment artifacts |
| GitHub Actions secret `DEPLOY_KNOCK_URL` in `esp_rgbww_firmware` | full URL `https://lightinator.de/deploy-knock-<hash>` | Used by the firmware CI deploy workflow to knock before SSH |
| GitHub Actions secret `DEPLOY_KNOCK_URL` in `esp_rgb_webapp2` | full URL `https://lightinator.de/deploy-knock-<hash>` | Used by the webapp CI deploy workflow to knock before SSH |

The hash is the same value in all three locations. If it is ever rotated, update
all three together then re-run `setup.sh`. `update.sh` does not read or modify the
knock configuration. The legacy gitignored checkout-local secret is accepted by
`setup.sh` with a migration warning; move it outside the checkout. Never copy a
rendered fail2ban filter back into Git: only placeholder templates are tracked.
Do not run deployment through a shell tracer or publish its server-side files.

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
| `DEPLOY_KNOCK_HASH` | `setup.sh` | 64-char hex, server-side only; never committed |
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

Clone the repo to the server and run the bootstrap script as root:

```bash
git clone https://github.com/pljakobs/lightinator_server.git \
    /home/github-deploy/lightinator-server

# Provision /etc/lightinator/knock-secret.env locally, outside Git.
# It must be root-owned, mode 600, and define DEPLOY_KNOCK_HASH.
# Do not put its actual value into a command saved in shell history.

export CERTBOT_EMAIL=admin@lightinator.de   # optional
bash /home/github-deploy/lightinator-server/deploy/setup.sh
```

`setup.sh` installs host prerequisites, creates the deploy user and monitoring
network, opens HTTP/HTTPS/SSH services, provisions TLS and configures the
Lightinator fail2ban templates. Its jails read the nginx host log directory
directly. Unrelated jails are retained. The demo-enabled WebUI package and any
installer images/manifests must already be published/provisioned separately.

After setup, add deploy public keys to `/home/github-deploy/.ssh/authorized_keys`.

## Updating

```bash
# First select a reviewed, committed revision in a clean server checkout.
# The script deliberately does not git pull, reset, or resolve production drift.
sudo bash /home/github-deploy/lightinator-server/deploy/update.sh
```

Both scripts share application deployment. Repeated updates with unchanged code,
dependencies and configuration do not reinstall dependencies, restart the API or
reload nginx. Updates never change fail2ban or the container definition.
The existing virtualenv (including a symlink) and API environment file are kept.
The standard deployment paths/user in the systemd unit remain the supported
production layout.

The legacy catalogue is imported only if the default database is absent, and is
never moved or overwritten. Existing databases receive schema initialization from
the API, not a repeated legacy import. A custom database path must be initialized
explicitly. Deployment state is private under `/var/lib/lightinator/deploy`.

The nginx configuration preserves the `/version.json` database projection.
Deployment validates nginx configuration, checks the demo directly and through
nginx, then verifies the landing-page link. Failure exits nonzero and restores
managed nginx/unit/page files; it does **not** roll back code, dependencies or
database schema. Before the first production update, reconcile the dirty checkout and record a known
good code revision. The mandatory snapshot below captures the pre-deployment
database using SQLite's backup API. A failed deployment
does not write the application success stamp, so rerunning retries activation.
The first rollout must be supervised; this is not a full GitOps release manager.

### Pre-deployment backup and restore

Both entry points require a successful private backup **before** installing
packages/dependencies or modifying configuration. Backups are retained under
`/var/lib/lightinator/backups/before-deploy-*`, outside Git and the webroot, in
mode-700 directories. Files are created with a private umask. They contain actual
server secrets, including the rendered knock filter: **never upload, commit or
attach these directories to CI jobs, GitHub issues or support reports**.

Each snapshot captures:

- The actual server checkout, including dirty/ignored code and local secrets.
- The virtualenv, including its external target when the checkout uses a symlink.
- A consistent SQLite backup of `VERSION_DB_PATH` from the API environment file,
  or the default checkout database.
- The landing page, flashing manifests, images, nginx configuration, API service
  unit, nginx Quadlet, Lightinator environment files and fail2ban configuration.
- API/nginx/fail2ban active/enabled state, deployment stamps, and firewalld's
  permanent configuration and runtime/permanent zone reports.

Published download trees, nginx logs, TLS private keys/certificates, Grafana and
InfluxDB data are excluded. Deployment does not replace these files; setup can
provision a missing TLS certificate but certificate issuance is not reversed.
OS packages, container image storage and full firewall-runtime state are not
snapshotted. This is **not a whole-host snapshot**.

For a standalone snapshot of the current production checkout (even when dirty):

```bash
sudo python3.11 /path/to/reviewed/deploy/recovery.py backup \
    --repo /home/github-deploy/lightinator-server \
    --home /home/github-deploy
```

The command prints the backup location and its restore command. To recover:

1. Pause firmware/WebUI CI and any other SSH publisher; keep an SSH session open.
2. Run the independent recovery copy from the chosen backup, **not** from the
   potentially broken application checkout:

   ```bash
   sudo python3.11 /var/lib/lightinator/backups/before-deploy-EXAMPLE/recovery.py \
       restore /var/lib/lightinator/backups/before-deploy-EXAMPLE \
       --confirm-publishers-paused
   ```

3. Recovery checks backup integrity and original hostname, stops the three
   services, retains the failed checkout/virtualenv in sibling
   `.before-restore-*` directories, restores the code/database/configuration,
   then reactivates services according to their saved state.
4. Verify website, catalogue, demo, SSH and fail2ban before resuming publishers.

Firewalld configuration is captured but **not automatically restored or reloaded**:
it is shared host state, and reloading could disrupt SSH or container networking.
Reconcile the saved zone reports and archived configuration manually if setup
changed firewall policy. Other newly created files outside the replaced
checkout/virtualenv are retained; OS package changes are not undone.

Backup failure aborts deployment. Snapshots have no automatic expiry: check free
disk space and remove only specifically selected obsolete snapshots after
successful rollout/recovery validation. The round-trip tests use a temporary
filesystem with mocked service commands; first production recovery must still be
supervised.

Validation (no root access or production connections required):

```bash
bash -n deploy/common.sh deploy/setup.sh deploy/update.sh
python -m unittest discover -s tests -v
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
