#!/usr/bin/with-contenv bashio
set -euo pipefail

export HA_CONFIG_PATH="/config"
export AUTOMATIONS_PATH="$(bashio::config 'automations_path')"

# Token regelen
if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
  export SUPERVISOR_TOKEN="$(bashio::config 'supervisor_token')"
fi

MARKER="/data/.initialized"

log_banner() {
  bashio::log.info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  bashio::log.info "🤖 Automation Maker — installatie cabaret editie"
  bashio::log.info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

first_run_show() {
  log_banner
  bashio::log.info "🎬 Première-avond! Dit is de allereerste start."
  bashio::log.info "🧙 Stap 1/6: Magische dependencies aaien... (heel voorzichtig)"
  sleep 0.35
  bashio::log.info "📦 Stap 2/6: Bestanden op precies de goede plek leggen (ongeveer)"
  sleep 0.35
  bashio::log.warning "⚠️ Stap 3/6: Kabouters wakker maken... ze hebben GEEN zin."
  sleep 0.35
  bashio::log.info "🧪 Stap 4/6: YAML temmen. Als het sist: dat is normaal."
  sleep 0.35
  bashio::log.info "🔧 Stap 5/6: Automations-pad inspecteren met een zaklamp 🔦"
  sleep 0.35
  bashio::log.info "✅ Stap 6/6: Klaar! Ik zet een marker zodat ik niet elke reboot een musical doe."
  date -Iseconds > "${MARKER}"
  bashio::log.info "🏁 First-run marker geschreven naar ${MARKER}"
}

normal_run_show() {
  bashio::log.info "🔁 Opstarten... (ik ben terug. Nog steeds zonder koffie.)"
}

# First run check
if [ ! -f "${MARKER}" ]; then
  first_run_show
else
  normal_run_show
fi

# Jouw bestaande logs (maar dan met iets meer flair)
bashio::log.info "📌 Automations path: ${AUTOMATIONS_PATH}"
bashio::log.info "🔑 Supervisor token available: $( [ -n "${SUPERVISOR_TOKEN:-}" ] && echo yes || echo no )"

# Kleine sanity-check met humor
if [ -z "${AUTOMATIONS_PATH}" ] || [ "${AUTOMATIONS_PATH}" = "null" ]; then
  bashio::log.warning "😬 automations_path lijkt leeg/null. Ik ga tóch starten, maar dit ruikt naar 'waarom werkt het niet?'."
fi

bashio::log.info "🚀 Starting Automation Maker... (houd je armen binnen de container a.u.b.)"
exec python3 /app.py
