#!/usr/bin/env bash
# deploy/setup.sh — idempotent full-server bootstrap for lightinator.de
#
# Reproduces the complete current setup from scratch on a fresh RHEL/Fedora host.
# Safe to re-run; all steps check before acting.
#
# Usage (run as root):
#   export DEPLOY_KNOCK_HASH=<64-char hex>   # from the DEPLOY_KNOCK_HASH server secret
#   bash /home/github-deploy/lightinator-server/deploy/setup.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${DEPLOY_USER:-github-deploy}"
DEPLOY_HOME="/home/${DEPLOY_USER}"
DOMAIN="${DOMAIN:-lightinator.de}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"

# ---------------------------------------------------------------------------
# Load port-knock secret
# ---------------------------------------------------------------------------

ENV_FILE="${REPO_DIR}/knock-secret.env"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
    echo "==> Loaded knock secret from ${ENV_FILE}"
elif [[ -z "${DEPLOY_KNOCK_HASH:-}" ]]; then
    echo "" >&2
    echo "ERROR: No knock secret found." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------

echo "==> Installing system packages..."
dnf install -y \
    podman \
    fail2ban \
    certbot \
    python3 \
    python3.11 \
    python3.11-pip \
    firewalld \
    git
echo "    done"

# ---------------------------------------------------------------------------
# 2. github-deploy user + nginx group
# ---------------------------------------------------------------------------

echo "==> Creating deploy user and groups..."
if ! getent group nginx &>/dev/null; then
    groupadd --system nginx
    echo "    created group nginx"
fi

if ! id "${DEPLOY_USER}" &>/dev/null; then
    useradd --system --home-dir "${DEPLOY_HOME}" --create-home \
        --shell /bin/bash --groups nginx "${DEPLOY_USER}"
    echo "    created user ${DEPLOY_USER}"
else
    usermod -aG nginx "${DEPLOY_USER}" 2>/dev/null || true
    echo "    user ${DEPLOY_USER} already exists"
fi

install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh"
touch "${DEPLOY_HOME}/.ssh/authorized_keys"
chmod 600 "${DEPLOY_HOME}/.ssh/authorized_keys"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh/authorized_keys"

# ---------------------------------------------------------------------------
# 3. Python Environment & FastAPI Dependencies
# ---------------------------------------------------------------------------

echo "==> Setting up version API Python environment..."
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
echo "    FastAPI/Uvicorn environment ready"

# ---------------------------------------------------------------------------
# 4. Firewall
# ---------------------------------------------------------------------------

echo "==> Configuring firewall..."
systemctl enable --now firewalld
for svc in http https ssh; do
    if ! firewall-cmd --query-service="${svc}" --permanent &>/dev/null; then
        firewall-cmd --add-service="${svc}" --permanent
        echo "    opened ${svc}"
    fi
done
firewall-cmd --reload
echo "    firewall configured"

# ---------------------------------------------------------------------------
# 5. Podman network
# ---------------------------------------------------------------------------

echo "==> Creating Podman network..."
if ! podman network exists monitoring-stack 2>/dev/null; then
    podman network create monitoring-stack
    echo "    created monitoring-stack network"
fi

# ---------------------------------------------------------------------------
# 6. TLS certificate via certbot
# ---------------------------------------------------------------------------

echo "==> Checking TLS certificate for ${DOMAIN}..."
if [[ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    if [[ -n "${CERTBOT_EMAIL}" ]]; then
        certbot certonly --standalone --non-interactive --agree-tos \
            --email "${CERTBOT_EMAIL}" -d "${DOMAIN}"
    else
        certbot certonly --standalone --non-interactive --agree-tos \
            --register-unsafely-without-email -d "${DOMAIN}"
    fi
fi
systemctl enable --now certbot-renew.timer

# ---------------------------------------------------------------------------
# 7. Nginx directory layout & Database initialization
# ---------------------------------------------------------------------------

echo "==> Setting up storage directories and database..."
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/conf.d"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/html"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/logs"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/html/downloads"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/html/downloads/firmware"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/html/downloads/webapps"

# Migration trigger if legacy version.json is present
LEGACY_JSON="${DEPLOY_HOME}/nginx/html/version.json"
TARGET_DB="${REPO_DIR}/firmware.db"

if [[ -f "${LEGACY_JSON}" ]]; then
    echo "==> Migrating legacy version.json to SQLite database..."
    runuser -u "${DEPLOY_USER}" -- "${REPO_DIR}/.venv/bin/python" \
        "${REPO_DIR}/migrate_to_sqlite.py" \
        --source "${LEGACY_JSON}" \
        --db "${TARGET_DB}"
    LEGACY_BACKUP="${LEGACY_JSON}.bak"
    if [[ -e "${LEGACY_BACKUP}" ]]; then
        LEGACY_BACKUP="$(mktemp "${LEGACY_JSON}.bak.XXXXXX")"
        rm "${LEGACY_BACKUP}"
    fi
    mv "${LEGACY_JSON}" "${LEGACY_BACKUP}"
    echo "    Migration complete. Legacy file backed up to ${LEGACY_BACKUP}"
fi

install -m 644 "${REPO_DIR}/containers/nginx.container" \
    /etc/containers/systemd/nginx.container

install -m 644 -o "${DEPLOY_USER}" -g nginx \
    "${REPO_DIR}/nginx/default.conf" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"
chcon --reference="${DEPLOY_HOME}/nginx/conf.d" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"

systemctl daemon-reload
systemctl enable --now nginx.service

# ---------------------------------------------------------------------------
# 8. fail2ban filters & action scripts
# ---------------------------------------------------------------------------

echo "==> Installing fail2ban configs..."
mkdir -p /etc/fail2ban/jail.d

for template in gh-knocker nginx-podlet-access; do
    src="${REPO_DIR}/fail2ban/filter.d/${template}.conf"
    dest="/etc/fail2ban/filter.d/${template}.conf"
    sed "s/%%DEPLOY_KNOCK_HASH%%/${DEPLOY_KNOCK_HASH}/g" "$src" > "$dest"
done

install -m 644 "${REPO_DIR}/fail2ban/action.d/f2b-whitelist.conf" \
    /etc/fail2ban/action.d/f2b-whitelist.conf
install -m 644 "${REPO_DIR}/fail2ban/jail.d/lightinator.conf" \
    /etc/fail2ban/jail.d/lightinator.conf

systemctl enable --now fail2ban
systemctl reload fail2ban || systemctl restart fail2ban

# ---------------------------------------------------------------------------
# 9. version-api systemd service installation
# ---------------------------------------------------------------------------

echo "==> Installing version-api service..."
install -m 644 "${REPO_DIR}/systemd/version-api.service" \
    /etc/systemd/system/version-api.service

systemctl daemon-reload
systemctl enable --now version-api.service
echo "    version-api service active"

echo ""
echo "========================================"
echo "  Setup complete for ${DOMAIN}"
echo "========================================"
