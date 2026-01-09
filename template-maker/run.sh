#!/usr/bin/with-contenv bashio 

# --- Hard requirements: ALL must be true ---
if [[ "${CONFIRM_BETA:-false}" != "true" ]]; then
  echo "❌ confirm_beta is NIET true."
  echo "😇 Ik start niet. Zet confirm_beta: true als je bewust chaos wilt."
  exit 1
fi

if [[ "${I_HAVE_A_BACKUP:-false}" != "true" ]]; then
  echo "❌ i_have_a_backup is NIET true."
  echo "🛟 Geen backup = geen start. Ga eerst even volwassen doen."
  exit 1
fi

if [[ "${I_WONT_COMPLAIN_ON_GITHUB:-false}" != "true" ]]; then
  echo "❌ i_wont_complain_on_github is NIET true."
  echo "🐙 GitHub is geen klaagmuur. Zet 'm op true of ga lekker naar buiten."
  exit 1
fi





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
