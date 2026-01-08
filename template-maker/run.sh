#!/usr/bin/with-contenv bashio

export HA_CONFIG_PATH="/config"
export TEMPLATES_PATH="/config/include/templates"
export SUPERVISOR_TOKEN=$(bashio::config 'supervisor_token')

bashio::log.info "Starting Template Maker..."
bashio::log.info "Config path: ${HA_CONFIG_PATH}"
bashio::log.info "Templates path: ${TEMPLATES_PATH}"

python3 /app.py
EOF
chmod +x run.sh
echo -e "${GREEN}✓${NC} run.sh aangemaakt"

echo ""
