#!/usr/bin/env bash
# ==============================================================================
# deploy/setup-vps.sh — Automated Production Installer for ChronoMed on Ubuntu
# ==============================================================================
# Wrapper that invokes /setup.sh or runs the standalone setup logic.
# ==============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_SETUP="${SCRIPT_DIR}/../setup.sh"

if [[ -f "${PARENT_SETUP}" ]]; then
  exec bash "${PARENT_SETUP}" "$@"
else
  echo "Error: setup.sh not found at ${PARENT_SETUP}" >&2
  exit 1
fi
