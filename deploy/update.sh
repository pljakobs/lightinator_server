#!/usr/bin/env bash
# Deploy the explicitly selected, reviewed checkout; never pull or reset it.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_clean_checkout
backup_system
deploy_application
