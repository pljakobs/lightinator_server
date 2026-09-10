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
# To generate a suitable hash:   openssl rand -hex 32
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${DEPLOY_USER:-github-deploy}"
DEPLOY_HOME="/home/${DEPLOY_USER}"
DOMAIN="${DOMAIN:-lightinator.de}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"   # set to enable --email flag for certbot

# ---------------------------------------------------------------------------
# Load port-knock secret (knock-secret.env is gitignored)
# ---------------------------------------------------------------------------

ENV_FILE="${REPO_DIR}/knock-secret.env"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
    echo "==> Loaded knock secret from ${ENV_FILE}"
elif [[ -z "${DEPLOY_KNOCK_HASH:-}" ]]; then
    echo "" >&2
    echo "ERROR: No knock secret found." >&2
    echo "" >&2
    echo "  ${ENV_FILE} does not exist and DEPLOY_KNOCK_HASH is not set." >&2
    echo "" >&2
    echo "  To create a new secret:" >&2
    echo "    1. Generate a hash:" >&2
    echo "         openssl rand -hex 32" >&2
    echo "    2. Save it to the (gitignored) env file:" >&2
    echo "         echo 'DEPLOY_KNOCK_HASH=<hash>' > ${ENV_FILE}" >&2
    echo "         chmod 600 ${ENV_FILE}" >&2
    echo "    3. Add the same value to GitHub Actions secrets in BOTH repos:" >&2
    echo "         esp_rgbww_firmware  -> secret DEPLOY_KNOCK_URL" >&2
    echo "         esp_rgb_webapp2     -> secret DEPLOY_KNOCK_URL" >&2
    echo "       The secret value must be the full URL:" >&2
    echo "         https://${DOMAIN}/deploy-knock-<hash>" >&2
    echo "    4. Re-run this script." >&2
    echo "" >&2
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
    # Ensure user is in nginx group
    usermod -aG nginx "${DEPLOY_USER}" 2>/dev/null || true
    echo "    user ${DEPLOY_USER} already exists"
fi

# SSH directory for deploy keys (CI will populate authorized_keys)
install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh"
touch "${DEPLOY_HOME}/.ssh/authorized_keys"
chmod 600 "${DEPLOY_HOME}/.ssh/authorized_keys"

# FastAPI/Uvicorn runtime for the version API
echo "==> Installing version API Python environment..."
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
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh/authorized_keys"
echo "    .ssh/authorized_keys ready (add deploy public keys manually)"

# ---------------------------------------------------------------------------
# 3. Firewall — ensure http/https open
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
# 4. Podman network (monitoring-stack)
# ---------------------------------------------------------------------------

echo "==> Creating Podman network..."
if ! podman network exists monitoring-stack 2>/dev/null; then
    podman network create monitoring-stack
    echo "    created monitoring-stack network"
else
    echo "    monitoring-stack network already exists"
fi

# ---------------------------------------------------------------------------
# 5. TLS certificate via certbot
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
    echo "    certificate issued"
else
    echo "    certificate already present"
fi

# Ensure certbot-renew.timer is enabled (ships with certbot package)
systemctl enable --now certbot-renew.timer
echo "    certbot-renew.timer enabled"

# ---------------------------------------------------------------------------
# 6. nginx directory layout + Quadlet
# ---------------------------------------------------------------------------

echo "==> Setting up nginx directories and Quadlet..."
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/conf.d"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/html"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/logs"
install -d -o "${DEPLOY_USER}" -g nginx -m 775 "${DEPLOY_HOME}/nginx/html/download"

# Minimal version.json stub (CI will overwrite with real data)
if [[ ! -f "${DEPLOY_HOME}/nginx/html/version.json" ]]; then
    echo '{"firmware":[],"webapp":[],"history":[],"webapp_history":[]}' \
        > "${DEPLOY_HOME}/nginx/html/version.json"
    chown "${DEPLOY_USER}:nginx" "${DEPLOY_HOME}/nginx/html/version.json"
    echo "    created stub version.json"
fi

install -m 644 "${REPO_DIR}/containers/nginx.container" \
    /etc/containers/systemd/nginx.container

install -m 644 -o "${DEPLOY_USER}" -g nginx \
    "${REPO_DIR}/nginx/default.conf" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"
chcon --reference="${DEPLOY_HOME}/nginx/conf.d" \
    "${DEPLOY_HOME}/nginx/conf.d/default.conf"
echo "    installed default.conf"

systemctl daemon-reload
systemctl enable --now nginx.service
echo "    nginx enabled and started"

# ---------------------------------------------------------------------------
# 7. fail2ban filters, actions, jail config
# ---------------------------------------------------------------------------

echo "==> Installing fail2ban configs..."
mkdir -p /etc/fail2ban/jail.d

for template in gh-knocker nginx-podlet-access; do
    src="${REPO_DIR}/fail2ban/filter.d/${template}.conf"
    dest="/etc/fail2ban/filter.d/${template}.conf"
    sed "s/%%DEPLOY_KNOCK_HASH%%/${DEPLOY_KNOCK_HASH}/g" "$src" > "$dest"
    echo "    wrote ${dest}"
done

install -m 644 "${REPO_DIR}/fail2ban/action.d/f2b-whitelist.conf" \
    /etc/fail2ban/action.d/f2b-whitelist.conf
install -m 644 "${REPO_DIR}/fail2ban/jail.d/lightinator.conf" \
    /etc/fail2ban/jail.d/lightinator.conf

systemctl enable --now fail2ban
systemctl reload fail2ban || systemctl restart fail2ban
echo "    fail2ban configured"

# ---------------------------------------------------------------------------
# 8. version-api systemd service
# ---------------------------------------------------------------------------

echo "==> Installing version-api service..."
install -m 644 "${REPO_DIR}/systemd/version-api.service" \
    /etc/systemd/system/version-api.service

systemctl daemon-reload
systemctl enable --now version-api.service
echo "    version-api enabled and started"

# ---------------------------------------------------------------------------

echo ""
echo "========================================"
echo "  Setup complete for ${DOMAIN}"
echo "========================================"
echo ""
echo "Manual steps remaining:"
echo "  1. Add deploy public keys to ${DEPLOY_HOME}/.ssh/authorized_keys"
echo "  2. Verify nginx:  curl -I https://${DOMAIN}/"
