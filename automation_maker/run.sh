#!/usr/bin/with-contenv bashio

export HA_CONFIG_PATH="/config"
export AUTOMATIONS_PATH="$(bashio::config 'automations_path')"

# 1) Als Supervisor al een token geeft: top.
# 2) Zo niet: pak 'm uit de add-on opties.
if [ -z "${SUPERVISOR_TOKEN}" ]; then
  export SUPERVISOR_TOKEN="$(bashio::config 'supervisor_token')"
fi

bashio::log.info "Starting Automation Maker..."
bashio::log.info "Automations path: ${AUTOMATIONS_PATH}"
bashio::log.info "Supervisor token available: $( [ -n "${SUPERVISOR_TOKEN}" ] && echo yes || echo no )"

python3 /app.py
