#!/usr/bin/env bash
# Shared application deployment for setup.sh and update.sh.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${DEPLOY_USER:-github-deploy}"
DEPLOY_HOME="${DEPLOY_HOME:-/home/${DEPLOY_USER}}"
SYSTEM_ROOT="${SYSTEM_ROOT:-}"
STATE_DIR="${SYSTEM_ROOT}/var/lib/lightinator/deploy"
WEB_ROOT="${DEPLOY_HOME}/nginx/html"
API_ENV="${SYSTEM_ROOT}/etc/lightinator/version-api.env"

load_knock_secret() {
    set +x
    local secret_file="${SYSTEM_ROOT}/etc/lightinator/knock-secret.env"
    if [[ -f "${secret_file}" ]]; then
        # shellcheck source=/dev/null
        source "${secret_file}"
    elif [[ -f "${REPO_DIR}/knock-secret.env" ]]; then
        echo "WARNING: Move knock-secret.env outside the checkout to /etc/lightinator/knock-secret.env." >&2
        # shellcheck source=/dev/null
        source "${REPO_DIR}/knock-secret.env"
    fi
    if [[ -n "${DEPLOY_KNOCK_HASH:-}" && ! "${DEPLOY_KNOCK_HASH}" =~ ^[a-fA-F0-9]{64}$ ]]; then
        echo "ERROR: The deploy-knock secret must be 64 hexadecimal characters." >&2
        return 1
    fi
}

require_clean_checkout() {
    if ! git -C "${REPO_DIR}" diff --quiet HEAD --; then
        echo "ERROR: Commit or reconcile tracked checkout changes before deployment." >&2
        return 1
    fi
}

install_changed() {
    local source="$1" destination="$2" mode="${3:-644}"
    if [[ -f "${destination}" ]] && cmp -s "${source}" "${destination}"; then
        return
    fi
    MANAGED_PATHS+=("${destination}")
    if [[ -e "${destination}" ]]; then
        cp -a -- "${destination}" "${BACKUP_DIR}/${#MANAGED_PATHS[@]}"
    fi
    install -m "${mode}" "${source}" "${destination}.lightinator-new"
    chown "${DEPLOY_USER}:nginx" "${destination}.lightinator-new"
    mv -f -- "${destination}.lightinator-new" "${destination}"
    FILE_CHANGED=1
}

rollback_managed_files() {
    local status=$? i destination
    trap - EXIT
    if [[ "${status}" -ne 0 ]]; then
        echo "ERROR: Deployment failed; restoring managed files. Application code and database are not rolled back." >&2
        for ((i=${#MANAGED_PATHS[@]}; i>0; i--)); do
            destination="${MANAGED_PATHS[i-1]}"
            if [[ -e "${BACKUP_DIR}/${i}" ]]; then
                cp -a -- "${BACKUP_DIR}/${i}" "${destination}"
            else
                rm -f -- "${destination}"
            fi
        done
        if [[ "${NGINX_CHANGED}" -eq 1 ]]; then
            podman exec nginx nginx -t && podman exec nginx nginx -s reload || echo "ERROR: Nginx recovery failed." >&2
        fi
        if [[ "${UNIT_CHANGED}" -eq 1 ]]; then
            systemctl daemon-reload || echo "ERROR: systemd recovery failed." >&2
        fi
    fi
    find "${BACKUP_DIR}" -maxdepth 1 -type f -delete
    rmdir -- "${BACKUP_DIR}"
    exit "${status}"
}

deploy_application() {
    install -d -o root -g "${DEPLOY_USER}" -m 750 "${SYSTEM_ROOT}/etc/lightinator"
    install -d -o root -g root -m 700 "${STATE_DIR}"
    install -d -o "${DEPLOY_USER}" -g nginx -m 755 "${DEPLOY_HOME}/nginx/conf.d" "${WEB_ROOT}"
    if [[ ! -e "${API_ENV}" ]]; then
        install -o root -g "${DEPLOY_USER}" -m 640 /dev/null "${API_ENV}"
    fi

    local python="${API_PYTHON:-$(command -v python3.11 || command -v python3)}"
    local venv="${REPO_DIR}/.venv" requirements_digest application_digest
    # Never remove an existing environment, including production's external symlink.
    if [[ ! -e "${venv}" && ! -L "${venv}" ]]; then
        runuser -u "${DEPLOY_USER}" -- "${python}" -m venv "${venv}"
    fi
    "${venv}/bin/python" -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
    requirements_digest="$(cat "${REPO_DIR}/requirements-api.txt"; "${venv}/bin/python" --version)"
    requirements_digest="$(printf '%s' "${requirements_digest}" | sha256sum)"
    if [[ ! -f "${STATE_DIR}/requirements" || "$(cat "${STATE_DIR}/requirements")" != "${requirements_digest}" ]]; then
        runuser -u "${DEPLOY_USER}" -- "${venv}/bin/pip" install \
            --disable-pip-version-check --quiet -r "${REPO_DIR}/requirements-api.txt"
        printf '%s' "${requirements_digest}" > "${STATE_DIR}/requirements"
    fi

    # Import the legacy catalogue only when bootstrapping a missing default DB.
    # Existing deployments migrate their schema through the API lifespan hook.
    if [[ ! -f "${REPO_DIR}/firmware.db" && -f "${WEB_ROOT}/version.json" ]]; then
        if grep -q '^VERSION_DB_PATH=' "${API_ENV}"; then
            echo "ERROR: Custom VERSION_DB_PATH requires explicit database initialization before deployment." >&2
            return 1
        fi
        runuser -u "${DEPLOY_USER}" -- "${venv}/bin/python" "${REPO_DIR}/migrate_to_sqlite.py" \
            --source "${WEB_ROOT}/version.json" --db "${REPO_DIR}/firmware.db" --asset-root "${WEB_ROOT}"
    fi

    BACKUP_DIR="$(mktemp -d "${STATE_DIR}/backup.XXXXXX")"
    MANAGED_PATHS=()
    NGINX_CHANGED=0
    UNIT_CHANGED=0
    trap rollback_managed_files EXIT

    FILE_CHANGED=0
    install_changed "${REPO_DIR}/nginx/default.conf" "${DEPLOY_HOME}/nginx/conf.d/default.conf"
    NGINX_CHANGED="${FILE_CHANGED}"
    if [[ "${NGINX_CHANGED}" -eq 1 ]]; then
        chcon --reference="${DEPLOY_HOME}/nginx/conf.d" "${DEPLOY_HOME}/nginx/conf.d/default.conf"
    fi
    FILE_CHANGED=0
    install_changed "${REPO_DIR}/systemd/version-api.service" "${SYSTEM_ROOT}/etc/systemd/system/version-api.service"
    UNIT_CHANGED="${FILE_CHANGED}"
    chown root:root "${SYSTEM_ROOT}/etc/systemd/system/version-api.service"
    podman exec nginx nginx -t

    # Verify the selected API and published demo before exposing the new link.
    application_digest="$(sha256sum "${REPO_DIR}/version_api.py" "${REPO_DIR}/requirements-api.txt" \
        "${REPO_DIR}/systemd/version-api.service" "${API_ENV}")"
    if [[ "${UNIT_CHANGED}" -eq 1 ]]; then
        systemctl daemon-reload
    fi
    systemctl enable version-api.service
    if [[ ! -f "${STATE_DIR}/application" || "$(cat "${STATE_DIR}/application")" != "${application_digest}" ]] \
        || ! systemctl is-active --quiet version-api.service; then
        systemctl restart version-api.service
    fi
    local attempt
    for attempt in {1..15}; do
        if curl --fail --silent --show-error --location --max-time 10 \
            http://127.0.0.1:8765/api/demo/ -o "${BACKUP_DIR}/demo.html"; then
            break
        fi
        if [[ "${attempt}" -eq 15 ]]; then
            echo "ERROR: Demo health check failed; publish a demo-enabled 5.1/develop WebUI first." >&2
            return 1
        fi
        sleep 1
    done
    if ! grep -q 'lightinator-demo' "${BACKUP_DIR}/demo.html"; then
        echo "ERROR: Demo health response is not a demo-enabled WebUI." >&2
        return 1
    fi
    if [[ "${NGINX_CHANGED}" -eq 1 ]]; then
        podman exec nginx nginx -s reload
    fi
    install_changed "${REPO_DIR}/html/index.html" "${WEB_ROOT}/index.html"
    chcon --reference="${WEB_ROOT}" "${WEB_ROOT}/index.html"
    curl --fail --silent --show-error --location --max-time 30 \
        -H 'Host: lightinator.de' http://127.0.0.1/api/demo/ -o "${BACKUP_DIR}/proxied-demo.html"
    if ! grep -q 'lightinator-demo' "${BACKUP_DIR}/proxied-demo.html"; then
        echo "ERROR: Nginx did not serve the demo-enabled WebUI." >&2
        return 1
    fi
    curl --fail --silent --show-error --max-time 10 \
        -H 'Host: lightinator.de' http://127.0.0.1/ -o "${BACKUP_DIR}/landing.html"
    if ! grep -q 'href="/api/demo/"' "${BACKUP_DIR}/landing.html"; then
        echo "ERROR: Nginx did not serve the updated landing page." >&2
        return 1
    fi
    printf '%s' "${application_digest}" > "${STATE_DIR}/application"
    echo "==> Demo deployment verified. Downloads, manifests, secrets and existing databases preserved."
}
