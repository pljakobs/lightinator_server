#!/usr/bin/env bash
# Bootstrap host prerequisites, then use the same application deploy as update.sh.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_clean_checkout
backup_system
load_knock_secret
if [[ -z "${DEPLOY_KNOCK_HASH:-}" ]]; then
    echo "ERROR: Configure /etc/lightinator/knock-secret.env before setup." >&2
    exit 1
fi

DOMAIN="${DOMAIN:-lightinator.de}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"

dnf install -y podman fail2ban certbot python3 python3.11 python3.11-pip firewalld git curl
if ! getent group nginx >/dev/null; then
    groupadd --system nginx
fi
if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${DEPLOY_HOME}" --create-home \
        --shell /bin/bash --groups nginx "${DEPLOY_USER}"
else
    usermod -aG nginx "${DEPLOY_USER}"
fi
install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh"
touch "${DEPLOY_HOME}/.ssh/authorized_keys"
chmod 600 "${DEPLOY_HOME}/.ssh/authorized_keys"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh/authorized_keys"

systemctl enable --now firewalld
FIREWALL_CHANGED=0
for service in http https ssh; do
    if ! firewall-cmd --query-service="${service}" --permanent >/dev/null; then
        firewall-cmd --add-service="${service}" --permanent
        FIREWALL_CHANGED=1
    fi
done
if [[ "${FIREWALL_CHANGED}" -eq 1 ]]; then
    firewall-cmd --reload
fi
if ! podman network exists monitoring-stack; then
    podman network create monitoring-stack
fi

if [[ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    CERTBOT_ARGS=(--register-unsafely-without-email)
    if [[ -n "${CERTBOT_EMAIL}" ]]; then
        CERTBOT_ARGS=(--email "${CERTBOT_EMAIL}")
    fi
    certbot certonly --standalone --non-interactive --agree-tos \
        "${CERTBOT_ARGS[@]}" -d "${DOMAIN}"
fi
systemctl enable --now certbot-renew.timer

install -d -m 755 "${SYSTEM_ROOT}/etc/containers/systemd"
install -d -o "${DEPLOY_USER}" -g nginx -m 755 \
    "${DEPLOY_HOME}/nginx/conf.d" "${WEB_ROOT}" "${DEPLOY_HOME}/nginx/logs"
if [[ ! -e "${DEPLOY_HOME}/nginx/conf.d/default.conf" ]]; then
    install -m 644 "${REPO_DIR}/nginx/default.conf" "${DEPLOY_HOME}/nginx/conf.d/default.conf"
    chcon --reference="${DEPLOY_HOME}/nginx/conf.d" "${DEPLOY_HOME}/nginx/conf.d/default.conf"
fi
if ! cmp -s "${REPO_DIR}/containers/nginx.container" "${SYSTEM_ROOT}/etc/containers/systemd/nginx.container"; then
    install -m 644 "${REPO_DIR}/containers/nginx.container" "${SYSTEM_ROOT}/etc/containers/systemd/nginx.container"
    systemctl daemon-reload
    systemctl restart nginx.service
fi
systemctl enable --now nginx.service

# Point our jails directly at the nginx host bind mount.
FAIL2BAN_CHANGED=0
SECRET_TEMP="$(mktemp)"
chmod 600 "${SECRET_TEMP}"
trap 'rm -f -- "${SECRET_TEMP}"' EXIT
for template in gh-knocker nginx-podlet-access; do
    # Render locally without putting the secret in a subprocess argument.
    while IFS= read -r line || [[ -n "${line}" ]]; do
        printf '%s\n' "${line//%%DEPLOY_KNOCK_HASH%%/${DEPLOY_KNOCK_HASH}}"
    done < "${REPO_DIR}/fail2ban/filter.d/${template}.conf" > "${SECRET_TEMP}"
    destination="${SYSTEM_ROOT}/etc/fail2ban/filter.d/${template}.conf"
    if ! cmp -s "${SECRET_TEMP}" "${destination}"; then
        install -m 600 "${SECRET_TEMP}" "${destination}"
        FAIL2BAN_CHANGED=1
    fi
done
for relative in action.d/f2b-whitelist.conf jail.d/lightinator.conf; do
    destination="${SYSTEM_ROOT}/etc/fail2ban/${relative}"
    sed "s|/var/log/nginx/access.log|${DEPLOY_HOME}/nginx/logs/access.log|g" \
        "${REPO_DIR}/fail2ban/${relative}" > "${SECRET_TEMP}"
    if ! cmp -s "${SECRET_TEMP}" "${destination}"; then
        install -m 644 "${SECRET_TEMP}" "${destination}"
        FAIL2BAN_CHANGED=1
    fi
done
rm -f -- "${SECRET_TEMP}"
trap - EXIT
fail2ban-client -t
systemctl enable --now fail2ban
if [[ "${FAIL2BAN_CHANGED}" -eq 1 ]]; then
    systemctl reload fail2ban
fi

deploy_application
