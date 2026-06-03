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

## Initial setup

Clone the repo to the server and run the bootstrap script as root:

```bash
git clone https://github.com/pjakobs/lightinator-server.git \
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

`version_api.py` exposes a read-only JSON API backed by `version.json`.
nginx proxies `/api/` to `http://127.0.0.1:8765/`.

Key endpoints:
```
GET /api/firmware/<soc>/latest/<branch>/<type>
GET /api/webapp/latest/<branch>?firmware_version=<ver>
GET /api/firmware/<soc>/branches
GET /api/webapp/branches
```

See [plan.md](plan.md) Phase 6 for the full URL structure and semantics.

## Port knocking

SSH access from GitHub Actions is gated by a fail2ban port-knock mechanism.
See [Secrets and keys → Port-knock secret](#port-knock-secret-deploy_knock_hash) above for the full description, rotation procedure, and where the secret is stored.
