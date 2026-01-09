#!/usr/bin/with-contenv bashio
set -e

export HA_CONFIG_PATH="/config"
export DASHBOARDS_PATH="/config/dashboards"

# Optioneel: zet poort expliciet voor de zekerheid (jouw app gebruikt default 5001)
export PORT="5001"

# Tokens (als je ze in add-on opties hebt)
ACCESS_TOKEN="$(bashio::config 'access_token')"
SUPERVISOR_TOKEN_CFG="$(bashio::config 'supervisor_token')"

if bashio::var.has_value "${ACCESS_TOKEN}"; then
  export HOMEASSISTANT_TOKEN="${ACCESS_TOKEN}"
fi

if bashio::var.has_value "${SUPERVISOR_TOKEN_CFG}"; then
  export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN_CFG}"
fi

bashio::log.info "Starting Dashboard Maker..."
bashio::log.info "HA_CONFIG_PATH=${HA_CONFIG_PATH}"
bashio::log.info "DASHBOARDS_PATH=${DASHBOARDS_PATH}"
bashio::log.info "PORT=${PORT}"
bashio::log.info "HOMEASSISTANT_TOKEN set: $(bashio::var.has_value "${HOMEASSISTANT_TOKEN}" && echo yes || echo no)"
bashio::log.info "SUPERVISOR_TOKEN set: $(bashio::var.has_value "${SUPERVISOR_TOKEN}" && echo yes || echo no)"

# Start app (foreground) zodat ingress kan verbinden
exec python3 -u /app.py
