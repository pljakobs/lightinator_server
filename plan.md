## Phase 6 - lightinator.de Version API

Replace direct `version.json` consumption by firmware and update UI with a thin read-only HTTP API backed by the same file. The full `version.json` stays as the CI-managed source of truth; the API exposes structured, size-appropriate views over it.

**Motivation**

- `version.json` is currently 88KB and growing unboundedly with history entries. Too large for ESP8266 to fetch and parse in RAM.
- The firmware update UI needs version browsing (select a specific version to flash), which static per-branch latest files cannot support without per-query filtering.
- Version strings are CI-generated and stable identifiers; using them directly in URL paths avoids the index-instability problem (stale index pointing to wrong version if a new build is deployed between the list fetch and the file fetch).

**URL structure**

All endpoints are read-only (GET). Version strings appear as path segments, not query indexes.

```
# Firmware
GET /api/firmware/<soc>/branches
    → ["experimental","devel","testing","stable"]

GET /api/firmware/<soc>/versions
    → [{"version":"V5.0-314-experimental","branch":"experimental","type":"debug","url":"..."},...]

GET /api/firmware/<soc>/versions/<branch>
    → filtered list for exact soc+branch, all types

GET /api/firmware/<soc>/versions/<branch>/<type>
    → filtered list for exact soc+branch+type combination

GET /api/firmware/<soc>/latest/<branch>/<type>
    → {"version":"V5.0-314-experimental","url":"http://lightinator.de/download/...rom0.bin"}
    → ~120 bytes; this is the only endpoint firmware needs to call

# Webapp (SOC-independent — one webapp serves all SOCs)
GET /api/webapp/branches
    → ["experimental","devel","testing","stable"]

GET /api/webapp/versions/<branch>?firmware_version=V5.0-314-experimental
    → [{"version":"V5.0-314-experimental","min_firmware":"5.0.0","files":[...]},...]
    → if firmware_version provided, only returns entries where min_firmware <= firmware_version
    → without firmware_version, returns all versions (for human browsing in the update UI)

GET /api/webapp/latest/<branch>?firmware_version=V5.0-314-experimental
    → {"version":"V5.0-314-experimental","min_firmware":"5.0.0","files":[{"path":"...","md5":"..."},...]}
    → returns the newest webapp version compatible with the given firmware version
    → 404 if no compatible version exists for this firmware
    → ~500 bytes; this is the only endpoint firmware needs to call for webapp refresh
    → firmware additionally applies its own min_webapp guard before activating a downloaded bundle
```

**Compatibility filtering semantics**

- Server filters: `min_firmware <= current_firmware_version` (server knows what the webapp requires)
- Firmware filters: `min_webapp <= installed_webapp_version` (firmware knows what it requires from the webapp)
- Neither side trusts the other blindly — both gates must pass before activation
- The `firmware_version` query parameter uses the same version string format as `fw_version` in `version.json`; the API reuses `parse_version_components()` from `manage_version.py` for comparison

**Implementation**

- Single Python file using FastAPI (or stdlib `http.server` if zero-dependency is preferred).
- Reads `version.json` from disk on each request (or caches with a short TTL, e.g. 10s).
- No database, no write path, no auth required (all data is already publicly served as static files).
- nginx: add `location /api/ { proxy_pass http://127.0.0.1:8765/; }` ahead of the existing static file block.
- systemd unit runs the process as `github-deploy` user, auto-restarts on failure.
- Deploy: managed by a dedicated server repo (see Phase 7 below).

**Relevant new files**

- `version_api.py` — the API process (lives in server repo, deployed to server)
- `version_api.service` — systemd unit
- `nginx_api.conf` — nginx location snippet

**Firmware impact**

Firmware calls `/api/webapp/latest/<branch>` instead of fetching `version.json`. Response is bounded, structured, and will not grow with history accumulation. The `cull` call in the deploy workflow can be relaxed or removed since history size no longer affects device behaviour.

**Update UI impact**

The update UI replaces its current full `version.json` fetch with:
1. `GET /api/firmware/<soc>/branches` to populate a branch selector (soc known from `/api/info`).
2. `GET /api/firmware/<soc>/versions/<branch>/<type>` to populate a version list.
3. Flash URL comes from the `url` field in the selected entry — no separate lookup needed.

---

## Phase 7 - lightinator.de as a Managed Component

lightinator.de is currently configured ad-hoc: nginx config, deploy keys, directory layout, and scripts are set up manually and not version-controlled. Adding `version_api.py` and its systemd unit makes the server a proper software component that needs to be managed like any other.

**Current server setup (as-found)**

- OS: RHEL-family Linux, systemd-managed
- nginx runs as a **Podman container** started by a systemd **Quadlet** unit (`/etc/containers/systemd/nginx.container`)
  - Image: `docker.io/nginx:latest` with `io.containers.autoupdate=image`
  - Ports: 80 and 443 published from host
  - Mounts:
    - `/home/github-deploy/nginx/conf.d` → `/etc/nginx/conf.d` (nginx config)
    - `/home/github-deploy/nginx/html` → `/usr/share/nginx/html` (served files)
    - `/home/github-deploy/nginx/logs` → `/var/log/nginx`
    - `/etc/letsencrypt` → `/etc/letsencrypt` (TLS certs, read-only)
  - Connected to `monitoring-stack` podman network (reaches Grafana)
- TLS: Let's Encrypt via `certbot-renew.timer` (auto-renew)
- Port knocking: fail2ban `gh-knocker` jail watches nginx access log for a GET to a secret URL (`/deploy-knock-<hash>`); matching IPs are whitelisted via `firewallcmd-ipset`
- Other fail2ban jails: `nginx-podlet-access` (brute force), `sshd` (aggressive mode), `recidive` (repeat offenders → 1 week ban)
- All served content owned by `github-deploy:nginx` under `/home/github-deploy/nginx/html/`
- Current files in html root: `version.json`, `manage_version.py`, `manage_versions.py`, `download/`, `index.html`, `robots.txt`
- Grafana and InfluxDB/Telegraf/Mosquitto also run on the host (monitoring stack, separate concern)

**Quadlet note**: nginx is already a Quadlet (the comment in the unit file says "should probably be a Quadlet in the future" but it already is one — `nginx.service` is generated from `/etc/containers/systemd/nginx.container`).

**Create a dedicated server repository (`lightinator-server`)**

Move everything server-side into one repo:

```
lightinator-server/
  manage_version.py              ← moved from esp_rgbww_firmware (canonical home)
  version_api.py                 ← new API process
  containers/
    nginx.container              ← current Quadlet unit (version-controlled)
  nginx/
    default.conf                 ← current hand-edited config (version-controlled)
  fail2ban/
    filter.d/gh-knocker.conf     ← port knock filter
    jail.d/50-nginx.conf         ← nginx + gh-knocker jail config
  systemd/
    version-api.service          ← new systemd unit for the API process
  deploy/
    setup.sh                     ← idempotent bootstrap (install deps, enable services)
    update.sh                    ← pull repo + restart affected services
  README.md
```

**CI changes**

- `esp_rgbww_firmware` and `esp_rgb_webapp2` deploy workflows: after rsync, call `ssh github-deploy@lightinator.de ~/lightinator-server/deploy/update.sh` to pull latest server repo and restart the API if changed.
- `manage_version.py` call sites in both workflows invoke the canonical copy at `/home/github-deploy/lightinator-server/manage_version.py` via SSH, rather than uploading their own copy each time.
- Alternatively (simpler transition): workflows continue uploading `manage_version.py` as today but the server repo carries the canonical version and a CI lint/test job validates it.

**What stays in `esp_rgbww_firmware`**

- Call sites in `.github/workflows/deploy-pages.yml` that invoke `manage_version.py` remotely.
- A pinned copy of `manage_version.py` for local development/testing only, with a note that the server copy is authoritative.

**What moves to `lightinator-server`**

- `manage_version.py` (canonical home)
- `version_api.py` (new)
- `nginx.container` Quadlet and `default.conf` (currently undocumented/hand-edited)
- fail2ban filter and jail configs
- systemd units for new services
- Any future server-side tooling

**Benefits**

- Server state is reproducible: a fresh VM can be provisioned from the repo.
- Changes to `manage_version.py` are reviewed and versioned independently of firmware releases.
- `version_api.py` and `manage_version.py` share a single Python environment and import path — no copy/sync problem.
- nginx config and Quadlet definition are auditable; accidental hand-edits are visible as uncommitted diffs.
- The port knock secret URL hash lives only in the fail2ban filter and the GitHub secret — the server repo documents the mechanism without exposing the secret.
