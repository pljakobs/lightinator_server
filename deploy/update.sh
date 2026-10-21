#!/usr/bin/env bash
# deploy/update.sh — pull latest repo, migrate schema, update virtualenv, restart services
# Called by CI after deploying new content, or manually after config updates.
# Must be run as root (or via sudo).
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${DEPLOY_USER:-github-deploy}"
DEPLOY_HOME="/home/${DEPLOY_USER}"

echo "==> Pulling latest git repository..."
git -C "${REPO_DIR}" pull --ff-only

ENV_FILE="${REPO_DIR}/knock-secret.env"
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
fi

# ---------------------------------------------------------------------------
# 1. Update Python dependencies & execute SQLite migrations if required
# ---------------------------------------------------------------------------

echo "==> Refreshing Python environment and dependencies..."
API_PYTHON="${API_PYTHON:-$(command -v python3.11 || command -v python3)}"
API_PYTHON_VERSION="$(${API_PYTHON} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
VENV_PYTHON_VERSION=""

if [[ -x "${REPO_DIR}/.venv/bin/python" ]]; then
    VENV_PYTHON_VERSION="$(${REPO_DIR}/.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
fi

if [[ "${VENV_PYTHON_VERSION}" != "${API_PYTHON_VERSION}" ]]; then
    rm -rf "${REPO_DIR}/.venv"
    runuser -u "${DEPLOY_USER}" -- "${API_PYTHON}" -m venv "${REPO_DIR}/.venv"
fi

runuser -u "${DEPLOY_USER}" -- "${REPO_DIR}/.venv/bin/pip" install \
    --disable-pip-version-check --quiet -r "${REPO_DIR}/requirements-api.txt"

LEGACY_JSON="${DEPLOY_HOME}/nginx/html/version.json"
TARGET_DB="${REPO_DIR}/firmware.db"

if [[ -f "${LEGACY_JSON}" ]]; then
    echo "==> Detected legacy version.json, running SQLite migration..."
    runuser -u "${DEPLOY_USER}" -- "${REPO_DIR}/.venv/bin/python" \
        "${REPO_DIR}/migrate_to_sqlite.py" \
        --source "${LEGACY_JSON}" \
        --db "${TARGET_DB}"
    mv "${LEGACY_JSON}" "${LEGACY_JSON}.bak"
    echo "    Migration completed successfully."
fi

# ---------------------------------------------------------------------------
# 2. Refresh fail2ban configuration
# ---------------------------------------------------------------------------

if [[ -n "${DEPLOY_KNOCK_HASH:-}" ]]; then
    echo "==> Refreshing fail2ban filters..."
    for template in gh-knocker nginx-podlet-access; do
        src="${REPO_DIR}/fail2ban/filter.d/${template}.conf"
        dest="/etc/fail2ban/filter.d/${template}.conf"
        sed "s/%%DEPLOY_KNOCK_HASH%%/${DEPLOY_KNOCK_HASH}/g" "$src" > "$dest"
    done
    install -m 644 "${REPO_DIR}/fail2ban/action.d/f2b-whitelist.conf" \
        /etc/fail2ban/action.d/f2b-whitelist.conf
    install -m 644 "${REPO_DIR}/fail2ban/jail.d/lightinator.conf" \
        /etc/fail2ban/jail.d/lightinator.conf
    systemctl reload fail2ban
fi

# ---------------------------------------------------------------------------
# 3. Reload Nginx configuration
# ---------------------------------------------------------------------------

echo "==> Synchronizing Nginx configuration..."
install -m 644 -o "${DEPLOY_USER}" -g nginx \
    "${REPO_DIR}/nginx/default.conf" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"
chcon --reference="${DEPLOY_HOME}/nginx/conf.d" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"

podman exec nginx nginx -t && podman exec nginx nginx -s reload

# ---------------------------------------------------------------------------
# 4. Restart version-api service
# ---------------------------------------------------------------------------

echo "==> Updating systemd service and restarting version-api..."
install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 700 /var/lib/lightinator/ota-artifacts
install -d -o root -g "${DEPLOY_USER}" -m 750 /etc/lightinator
if [[ ! -f /etc/lightinator/version-api.env ]]; then
    touch /etc/lightinator/version-api.env
fi
chown "root:${DEPLOY_USER}" /etc/lightinator/version-api.env
chmod 640 /etc/lightinator/version-api.env
install -m 644 "${REPO_DIR}/systemd/version-api.service" \
    /etc/systemd/system/version-api.service
systemctl daemon-reload
systemctl restart version-api.service

echo "==> Deployment update successfully completed."


