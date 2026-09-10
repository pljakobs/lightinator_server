#!/usr/bin/env bash
# deploy/update.sh — pull latest repo and restart affected services
# Called by CI after deploying new content, or manually after config edits.
# Must be run as root (or with sudo) on the server.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${DEPLOY_USER:-github-deploy}"
DEPLOY_HOME="/home/${DEPLOY_USER}"

echo "==> Pulling latest repo..."
git -C "${REPO_DIR}" pull --ff-only

# Load knock secret from env file if present (env var also accepted)
ENV_FILE="${REPO_DIR}/knock-secret.env"
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
fi

# ---------------------------------------------------------------------------
# Redeploy fail2ban filters if DEPLOY_KNOCK_HASH is set
# ---------------------------------------------------------------------------

if [[ -n "${DEPLOY_KNOCK_HASH:-}" ]]; then
    echo "==> Refreshing fail2ban filters..."
    for template in gh-knocker nginx-podlet-access; do
        src="${REPO_DIR}/fail2ban/filter.d/${template}.conf"
        dest="/etc/fail2ban/filter.d/${template}.conf"
        sed "s/%%DEPLOY_KNOCK_HASH%%/${DEPLOY_KNOCK_HASH}/g" "$src" > "$dest"
        echo "    wrote $dest"
    done
    install -m 644 "${REPO_DIR}/fail2ban/action.d/f2b-whitelist.conf" \
        /etc/fail2ban/action.d/f2b-whitelist.conf
    install -m 644 "${REPO_DIR}/fail2ban/jail.d/lightinator.conf" \
        /etc/fail2ban/jail.d/lightinator.conf
    systemctl reload fail2ban
    echo "    fail2ban reloaded"
fi

# ---------------------------------------------------------------------------
# Refresh nginx config and reload
# ---------------------------------------------------------------------------

echo "==> Refreshing nginx config..."
install -m 644 -o "${DEPLOY_USER}" -g nginx \
    "${REPO_DIR}/nginx/default.conf" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"
# Restore SELinux context so the nginx container (with container_file_t) can read it
restorecon "${DEPLOY_HOME}/nginx/conf.d/default.conf" 2>/dev/null || \
    chcon --reference="${DEPLOY_HOME}/nginx/conf.d/" \
          "${DEPLOY_HOME}/nginx/conf.d/default.conf"
podman exec nginx nginx -t && podman exec nginx nginx -s reload
echo "    nginx reloaded"

# ---------------------------------------------------------------------------
# Restart version-api if source changed
# ---------------------------------------------------------------------------

echo "==> Updating version API Python environment..."
if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
    runuser -u "${DEPLOY_USER}" -- python3 -m venv "${REPO_DIR}/.venv"
fi
runuser -u "${DEPLOY_USER}" -- "${REPO_DIR}/.venv/bin/pip" install \
    --disable-pip-version-check --quiet -r "${REPO_DIR}/requirements-api.txt"
echo "    Python dependencies updated"

echo "==> Restarting version-api..."
systemctl restart version-api.service
echo "    version-api restarted"

echo ""
echo "Update complete."
