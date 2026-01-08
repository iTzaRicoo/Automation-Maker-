#!/usr/bin/with-contenv bashio

export HA_CONFIG_PATH="/config"
export DASHBOARD_PATH="/config/include/templates"
export SUPERVISOR_TOKEN=$(bashio::config 'supervisor_token')

bashio::log.info "Starting Dashboard Maker..."
bashio::log.info "Config path: ${HA_CONFIG_PATH}"
bashio::log.info "Dashboard path: ${DASHBOARD_PATH}"

python3 /app.py
EOF
chmod +x run.sh
echo -e "${GREEN}✓${NC} run.sh aangemaakt"

echo ""
