#!/usr/bin/with-contenv bashio
set -euo pipefail

# --- Hard requirements: ALL must be true ---
if [[ "$(bashio::config 'confirm_beta')" != "true" ]]; then
  bashio::log.error "❌ confirm_beta is NIET true."
  bashio::log.error "😇 Ik start niet. Zet confirm_beta: true als je bewust chaos wilt."
  exit 1
fi

if [[ "$(bashio::config 'i_have_a_backup')" != "true" ]]; then
  bashio::log.error "❌ i_have_a_backup is NIET true."
  bashio::log.error "🛟 Geen backup = geen start. Ga eerst even volwassen doen."
  exit 1
fi

if [[ "$(bashio::config 'i_wont_complain_on_github')" != "true" ]]; then
  bashio::log.error "❌ i_wont_complain_on_github is NIET true."
  bashio::log.error "🐙 GitHub is geen klaagmuur. Zet 'm op true of ga lekker naar buiten."
  exit 1
fi

# --- Paths ---
export HA_CONFIG_PATH="/config"
export TEMPLATES_PATH="/config/include/templates"

# Ensure templates dir exists
mkdir -p "${TEMPLATES_PATH}"

# --- Token (prefer config value; fallback to supervisor file) ---
SUP_TOKEN="$(bashio::config 'supervisor_token' || true)"
if [[ -z "${SUP_TOKEN}" ]]; then
  if [[ -f /run/supervisor_token ]]; then
    SUP_TOKEN="$(cat /run/supervisor_token | tr -d '\r\n' || true)"
  elif [[ -f /var/run/supervisor_token ]]; then
    SUP_TOKEN="$(cat /var/run/supervisor_token | tr -d '\r\n' || true)"
  fi
fi
export SUPERVISOR_TOKEN="${SUP_TOKEN:-}"

bashio::log.info "Starting Template Maker..."
bashio::log.info "Config path: ${HA_CONFIG_PATH}"
bashio::log.info "Templates path: ${TEMPLATES_PATH}"
bashio::log.info "Token available: $([[ -n "${SUPERVISOR_TOKEN}" ]] && echo true || echo false)"

# --- Start app (foreground) ---
exec python3 /app.py
