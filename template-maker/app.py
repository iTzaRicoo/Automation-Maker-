#!/usr/bin/env python3
from __future__ import annotations

from flask import Flask, request, jsonify, Response
import yaml
import os
import re
from pathlib import Path
import requests
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

APP_VERSION = "1.2.0-beta-ready"
APP_NAME = "Template Maker Pro"

app = Flask(__name__)

# Paths
HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
TEMPLATES_PATH = os.environ.get("TEMPLATES_PATH") or os.path.join(HA_CONFIG_PATH, "include", "templates")

# Token discovery (HAOS add-on)
def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""

def discover_token() -> str:
    # 1) Official supervisor injected env var for add-ons
    tok = (os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()
    if tok:
        return tok

    # 2) Some bases expose HOMEASSISTANT_TOKEN
    tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()
    if tok:
        return tok

    # 3) Common location inside add-on containers
    tok = _read_file("/var/run/supervisor_token")
    if tok:
        return tok

    # 4) Old/alt locations (harmless if missing)
    tok = _read_file("/run/supervisor_token")
    if tok:
        return tok

    return ""

SUPERVISOR_TOKEN = discover_token()

Path(TEMPLATES_PATH).mkdir(parents=True, exist_ok=True)

print(f"== {APP_NAME} {APP_VERSION} ==")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Templates path: {TEMPLATES_PATH}")
print(f"Token available: {bool(SUPERVISOR_TOKEN)}")
print(f"Token sources: env(SUPERVISOR_TOKEN)={bool((os.environ.get('SUPERVISOR_TOKEN','') or '').strip())}, "
      f"env(HOMEASSISTANT_TOKEN)={bool((os.environ.get('HOMEASSISTANT_TOKEN','') or '').strip())}, "
      f"file(/var/run/supervisor_token)={bool(_read_file('/var/run/supervisor_token'))}")

# -------------------------
# Helpers
# -------------------------

def sanitize_filename(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "_", name)
    if not name:
        name = "unnamed"
    return name[:80]

def sanitize_entity_id(e: str) -> Optional[str]:
    if not isinstance(e, str):
        return None
    e = e.strip()
    if not e or "." not in e:
        return None
    if not re.match(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$", e):
        return None
    return e

def ha_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }

def safe_yaml_dump(obj: Any) -> str:
    """Dump YAML with multiline strings using | style."""
    class Dumper(yaml.SafeDumper):
        pass

    def str_presenter(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    Dumper.add_representer(str, str_presenter)
    return yaml.dump(obj, Dumper=Dumper, default_flow_style=False, allow_unicode=True, sort_keys=False)

def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_text_file(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def is_safe_filename(filename: str) -> bool:
    if not filename or not filename.endswith(".yaml"):
        return False
    if ".." in filename or "/" in filename or "\\" in filename:
        return False
    return bool(re.match(r"^[a-zA-Z0-9._-]+\.yaml$", filename))

def list_yaml_files(dir_path: str) -> List[str]:
    if not os.path.exists(dir_path):
        return []
    out = []
    for fn in os.listdir(dir_path):
        if fn.endswith(".yaml") and is_safe_filename(fn):
            out.append(fn)
    return sorted(out)

def next_available_filename(base_dir: str, desired: str) -> str:
    if not desired.endswith(".yaml"):
        desired += ".yaml"
    if not os.path.exists(os.path.join(base_dir, desired)):
        return desired
    stem = desired[:-5]
    for i in range(2, 999):
        cand = f"{stem}_{i}.yaml"
        if not os.path.exists(os.path.join(base_dir, cand)):
            return cand
    return f"{stem}_{int(datetime.now().timestamp())}.yaml"

# -------------------------
# Home Assistant API calls (Supervisor proxy)
# -------------------------

def ha_get_states() -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], int]:
    """
    Returns (states, error, http_status_for_error).
    """
    if not SUPERVISOR_TOKEN:
        return None, "Geen token gevonden in container (SUPERVISOR_TOKEN/HOMEASSISTANT_TOKEN of supervisor_token file).", 400
    try:
        url = "http://supervisor/core/api/states"
        resp = requests.get(url, headers=ha_headers(), timeout=12)
        if resp.status_code != 200:
            return None, f"HA states fetch failed: {resp.status_code}", resp.status_code
        return resp.json(), None, 200
    except Exception as e:
        return None, str(e), 500

def ha_template_render(template_str: str, variables: dict | None = None) -> Tuple[Dict[str, Any], int]:
    if not SUPERVISOR_TOKEN:
        return {"ok": False, "error": "Geen token in container. Dit is geen app.py probleem: je add-on krijgt het token niet geïnjecteerd."}, 400

    payload = {"template": template_str}
    if variables:
        payload["variables"] = variables

    try:
        # Supervisor proxy to Core endpoint
        resp = requests.post("http://supervisor/core/api/template", headers=ha_headers(), json=payload, timeout=15)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HA template test failed: {resp.status_code}", "details": resp.text}, 400
        return {"ok": True, "result": resp.text}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

def ha_call_service(domain: str, service: str, data: dict | None = None) -> Tuple[Dict[str, Any], int]:
    if not SUPERVISOR_TOKEN:
        return {"ok": False, "error": "Geen token in container. Kan geen service call doen."}, 400
    data = data or {}
    try:
        url = f"http://supervisor/core/api/services/{domain}/{service}"
        resp = requests.post(url, headers=ha_headers(), json=data, timeout=15)
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"Service call failed: {resp.status_code}", "details": resp.text}, 400
        # Some services return JSON list
        try:
            return {"ok": True, "result": resp.json()}, 200
        except Exception:
            return {"ok": True, "result": resp.text}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# -------------------------
# Template Builders
# -------------------------

def entities_to_jinja_list(entities: List[str]) -> str:
    safe = []
    for e in (entities or []):
        se = sanitize_entity_id(e)
        if se:
            safe.append(se)
    inner = ", ".join([f"'{x}'" for x in safe])
    return f"[{inner}]"

def build_threshold_state(entities: List[str], threshold: float, mode: str) -> str:
    lst = entities_to_jinja_list(entities)
    base = f"{lst} | map('states') | reject('in',['unknown','unavailable']) | map('float', 0) | list"
    if mode == "all":
        return f"{{{{ ({base} | select('gt', {threshold}) | list | count) == ({base} | length) and ({base} | length) > 0 }}}}"
    return f"{{{{ ({base} | select('gt', {threshold}) | list | count) > 0 }}}}"

def build_any_state_match(entities: List[str], states: List[str]) -> str:
    lst = entities_to_jinja_list(entities)
    st = "[" + ", ".join([f"'{s}'" for s in states]) + "]"
    return f"{{{{ ({lst} | map('states') | select('in', {st}) | list | count) > 0 }}}}"

def build_last_changed_human(entity_id: str) -> str:
    return (
        "{% set e = '" + entity_id + "' %}"
        "{{ as_local(states[e].last_changed).strftime('%Y-%m-%d %H:%M:%S') }}"
    )

def build_state_age_minutes(entity_id: str) -> str:
    return (
        "{% set e = '" + entity_id + "' %}"
        "{{ ((now() - states[e].last_changed).total_seconds() / 60) | round(0) }}"
    )

def build_unavailable_count(domain: str) -> str:
    return f"{{{{ states.{domain} | selectattr('state','in',['unknown','unavailable']) | list | count }}}}"

# Template catalog
TEMPLATE_CATALOG: Dict[str, Dict[str, Any]] = {}

def add_template(key: str, spec: Dict[str, Any]):
    TEMPLATE_CATALOG[key] = spec

def basic_sensor(name: str, uid: str, state: str, icon: str = "", extra: Dict[str, Any] | None = None):
    s = {"name": name, "unique_id": uid, "state": state}
    if icon:
        s["icon"] = icon
    if extra:
        s.update(extra)
    return {"template": [{"sensor": [s]}]}

def basic_binary(name: str, uid: str, state: str, icon: str = "", extra: Dict[str, Any] | None = None):
    b = {"name": name, "unique_id": uid, "state": state}
    if icon:
        b["icon"] = icon
    if extra:
        b.update(extra)
    return {"template": [{"binary_sensor": [b]}]}

# ---- common templates ----
add_template("count_lights", {
    "title": "💡 Tel lampen aan",
    "kind": "sensor",
    "needs_entities": False,
    "params": [],
    "defaults": {"icon": "mdi:lightbulb-group"},
    "suggestions": ["Perfect voor dashboard: als > 0, dan heb je nog lampen aan staan."],
    "entity_filter": {"domains": []},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        "{{ states.light | selectattr('state', 'eq', 'on') | list | count }}",
        "mdi:lightbulb-group",
        {"unit_of_measurement": "lampen"}
    ),
})

add_template("sum_power", {
    "title": "⚡ Som: totaal vermogen (W)",
    "kind": "sensor",
    "needs_entities": True,
    "params": [{"key": "round", "label": "Afronden (decimalen)", "type": "int", "default": 2}],
    "defaults": {"icon": "mdi:flash"},
    "suggestions": ["Selecteer sensoren met W (power). Unknown/unavailable wordt genegeerd."],
    "entity_filter": {"domains": ["sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        "{{ " + entities_to_jinja_list(entities or []) + " | map('states') | reject('in',['unknown','unavailable']) | map('float', 0) | sum | round(" + str(int(p.get("round", 2))) + ") }}",
        "mdi:flash",
        {"unit_of_measurement": "W", "device_class": "power"}
    ),
})

add_template("sum_energy", {
    "title": "🔌 Som: totaal energie (kWh)",
    "kind": "sensor",
    "needs_entities": True,
    "params": [{"key": "round", "label": "Afronden (decimalen)", "type": "int", "default": 3}],
    "defaults": {"icon": "mdi:transmission-tower"},
    "suggestions": ["Selecteer energy sensoren (kWh). Voor Energy dashboard: state_class total_increasing."],
    "entity_filter": {"domains": ["sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        "{{ " + entities_to_jinja_list(entities or []) + " | map('states') | reject('in',['unknown','unavailable']) | map('float', 0) | sum | round(" + str(int(p.get("round", 3))) + ") }}",
        "mdi:transmission-tower",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
    ),
})

add_template("average_temp", {
    "title": "🌡️ Gemiddelde temperatuur (°C)",
    "kind": "sensor",
    "needs_entities": True,
    "params": [{"key": "round", "label": "Afronden (decimalen)", "type": "int", "default": 1}],
    "defaults": {"icon": "mdi:thermometer"},
    "suggestions": ["Selecteer temperatuur sensoren."],
    "entity_filter": {"domains": ["sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        "{{ " + entities_to_jinja_list(entities or []) + " | map('states') | reject('in',['unknown','unavailable']) | map('float', 0) | average | round(" + str(int(p.get("round", 1))) + ") }}",
        "mdi:thermometer",
        {"unit_of_measurement": "°C", "device_class": "temperature"}
    ),
})

add_template("any_open", {
    "title": "🚪 Iets open? (binary sensor)",
    "kind": "binary_sensor",
    "needs_entities": True,
    "params": [],
    "defaults": {"icon": "mdi:door-open"},
    "suggestions": ["Selecteer deur/raam binary_sensors. True als één 'on' of 'open' is."],
    "entity_filter": {"domains": ["binary_sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_binary(
        name, uid,
        "{{ (" + entities_to_jinja_list(entities or []) + " | map('states') | select('in',['on','open']) | list | count) > 0 }}",
        "mdi:door-open",
        {"device_class": "door"}
    ),
})

add_template("threshold_above", {
    "title": "📈 Drempel: boven waarde? (binary sensor)",
    "kind": "binary_sensor",
    "needs_entities": True,
    "params": [
        {"key": "threshold", "label": "Drempelwaarde", "type": "float", "default": 100},
        {"key": "mode", "label": "Mode", "type": "select", "options": ["any", "all"], "default": "any"}
    ],
    "defaults": {"icon": "mdi:alert"},
    "suggestions": ["Power > 2000W, humidity > 60%, etc. any/all bepaalt of 1 genoeg is of allemaal."],
    "entity_filter": {"domains": ["sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_binary(
        name, uid,
        build_threshold_state(entities or [], float(p.get("threshold", 100)), p.get("mode", "any")),
        "mdi:alert"
    ),
})

add_template("unavailable_count_domain", {
    "title": "🚫 Aantal unavailable/unknown (per domein)",
    "kind": "sensor",
    "needs_entities": False,
    "params": [{"key": "domain", "label": "Domein", "type": "select",
                "options": ["sensor", "light", "switch", "binary_sensor", "climate", "media_player"], "default": "sensor"}],
    "defaults": {"icon": "mdi:alert-circle"},
    "suggestions": ["Handig om integratieproblemen snel te zien."],
    "entity_filter": {"domains": []},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        build_unavailable_count(p.get("domain", "sensor")),
        "mdi:alert-circle"
    ),
})

add_template("last_changed_human", {
    "title": "🕒 Laatste wijziging (human) — 1 entity",
    "kind": "sensor",
    "needs_entities": True,
    "params": [],
    "defaults": {"icon": "mdi:clock-outline"},
    "suggestions": ["Selecteer precies 1 entity."],
    "entity_filter": {"domains": []},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        build_last_changed_human((entities or [""])[0]),
        "mdi:clock-outline"
    ),
})

add_template("age_minutes", {
    "title": "⏱️ Minuten sinds laatste wijziging — 1 entity",
    "kind": "sensor",
    "needs_entities": True,
    "params": [],
    "defaults": {"icon": "mdi:timer-outline"},
    "suggestions": ["Selecteer precies 1 entity."],
    "entity_filter": {"domains": []},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        build_state_age_minutes((entities or [""])[0]),
        "mdi:timer-outline",
        {"unit_of_measurement": "min"}
    ),
})

# -------------------------
# Building logic
# -------------------------

def build_template_config(
    template_type: str,
    name: str,
    safe_name: str,
    icon: str,
    entities: List[str],
    params: Dict[str, Any],
):
    if template_type not in TEMPLATE_CATALOG:
        return None, "Ongeldig template type"

    spec = TEMPLATE_CATALOG[template_type]
    uid = f"template_{safe_name}"

    entities = [e for e in (entities or []) if sanitize_entity_id(e)]

    if spec.get("needs_entities"):
        if not entities:
            return None, "Deze template heeft een selectie van entities nodig."
        if template_type in ("last_changed_human", "age_minutes") and len(entities) != 1:
            return None, "Selecteer precies 1 entity voor dit template type."

    cfg = spec["builder"](name, uid, params, entities=entities)

    if icon:
        try:
            block = cfg["template"][0]
            if "sensor" in block and block["sensor"]:
                block["sensor"][0]["icon"] = icon
            if "binary_sensor" in block and block["binary_sensor"]:
                block["binary_sensor"][0]["icon"] = icon
        except Exception:
            pass

    return cfg, None

def extract_first_state_template(cfg: dict) -> Optional[str]:
    try:
        block = cfg["template"][0]
        if "sensor" in block and block["sensor"]:
            return block["sensor"][0].get("state")
        if "binary_sensor" in block and block["binary_sensor"]:
            return block["binary_sensor"][0].get("state")
    except Exception:
        return None
    return None

def extract_entity_info(cfg: dict) -> Tuple[str, str]:
    try:
        block = cfg["template"][0]
        if "sensor" in block and block["sensor"]:
            return ("sensor", block["sensor"][0].get("unique_id", ""))
        if "binary_sensor" in block and block["binary_sensor"]:
            return ("binary_sensor", block["binary_sensor"][0].get("unique_id", ""))
    except Exception:
        pass
    return ("", "")

def build_automation_snippet(entity_name: str, unique_id: str, kind: str, trigger_mode: str, threshold: Optional[float] = None) -> dict:
    alias = f"Reageer op {entity_name}"
    guessed_entity_id = f"{kind}.{unique_id.replace('template_', '')}"

    if trigger_mode == "numeric_state":
        th = 0 if threshold is None else threshold
        return {
            "alias": alias,
            "mode": "single",
            "trigger": [{"platform": "numeric_state", "entity_id": guessed_entity_id, "above": th}],
            "action": [{"service": "persistent_notification.create", "data": {"title": "Template Maker", "message": f"{entity_name} is boven {th}!"}}]
        }

    return {
        "alias": alias,
        "mode": "single",
        "trigger": [{"platform": "state", "entity_id": guessed_entity_id}],
        "action": [{"service": "persistent_notification.create", "data": {"title": "Template Maker", "message": f"{entity_name} veranderde van state."}}]
    }

# -------------------------
# UI
# -------------------------

@app.route("/")
def index():
    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{APP_NAME}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-br from-purple-50 to-blue-100 min-h-screen p-4">
  <div class="max-w-7xl mx-auto">
    <div class="bg-white rounded-2xl shadow-2xl p-8 mb-6">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
        <div>
          <h1 class="text-4xl font-bold text-purple-800">🎨 {APP_NAME}</h1>
          <p class="text-gray-600 mt-2">Alles-in-één template generator + test tool.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">{APP_VERSION}</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinding maken...</span>
          </div>
          <button onclick="reloadTemplatesInHA()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
            🔄 Reload Template Entities (HA)
          </button>
        </div>
      </div>

      <div id="tokenWarning" class="hidden mb-6 bg-yellow-50 border-l-4 border-yellow-400 p-4 rounded">
        <div class="flex">
          <div class="flex-shrink-0">⚠️</div>
          <div class="ml-3">
            <p class="text-sm text-yellow-700">
              <strong>Geen token in container!</strong><br>
              Genereren/opslaan werkt, maar <strong>testen/reload</strong> kan pas als je add-on correct SUPERVISOR_TOKEN krijgt.
            </p>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <div class="mb-4">
            <label class="block text-lg font-semibold text-gray-700 mb-2">📝 Naam</label>
            <input type="text" id="templateName" placeholder="bijv. Lampen Teller"
                   class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-purple-500 focus:outline-none">
          </div>

          <div class="mb-4">
            <label class="block text-lg font-semibold text-gray-700 mb-2">🎯 Type</label>
            <select id="templateType" onchange="onTypeChange()"
                    class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-purple-500 focus:outline-none">
              <option value="">-- Kies een template type --</option>
            </select>
          </div>

          <div class="mb-4">
            <label class="block text-lg font-semibold text-gray-700 mb-2">🎨 Icon (mdi)</label>
            <input type="text" id="templateIcon" placeholder="bijv. mdi:lightbulb-group"
                   class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-purple-500 focus:outline-none">
            <p class="text-xs text-gray-500 mt-1">Laat leeg voor standaard per type.</p>
          </div>

          <div class="mb-4 bg-gray-50 border border-gray-200 p-4 rounded-xl">
            <div class="flex items-center justify-between gap-3">
              <div class="font-semibold text-gray-800">💾 Opslag</div>
              <label class="flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" id="singleFileMode" class="scale-110">
                Alles in 1 bestand (<span class="font-mono">template_maker.yaml</span>)
              </label>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
              <label class="flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" id="overwriteMode" class="scale-110">
                Overschrijven als bestaat
              </label>
              <label class="flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" id="autoSuffixMode" class="scale-110" checked>
                Anders auto suffix (_2, _3)
              </label>
            </div>
          </div>

          <div id="suggestionsBox" class="mb-4 hidden bg-purple-50 border border-purple-200 p-4 rounded-xl">
            <div class="font-semibold text-purple-800 mb-2">💡 Suggesties</div>
            <ul id="suggestionsList" class="list-disc ml-5 text-sm text-purple-900 space-y-1"></ul>
          </div>

          <div id="paramsBox" class="mb-4 hidden bg-gray-50 border border-gray-200 p-4 rounded-xl">
            <div class="font-semibold text-gray-800 mb-2">⚙️ Opties</div>
            <div id="paramsFields" class="space-y-3"></div>
          </div>

          <div id="entitiesBox" class="mb-4 hidden bg-gray-50 border border-gray-200 p-4 rounded-xl">
            <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-2">
              <div>
                <div class="font-semibold text-gray-800">🧩 Entities</div>
                <div class="text-xs text-gray-500" id="entitiesHint"></div>
              </div>
              <div class="flex gap-2">
                <button onclick="selectAll()" class="text-xs bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">Select all</button>
                <button onclick="clearAll()" class="text-xs bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">Clear</button>
              </div>
            </div>

            <input id="entitySearch" oninput="renderEntities()" placeholder="Zoek entity (naam of entity_id)..."
                   class="w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-purple-500 focus:outline-none mb-3">

            <div id="selectedChips" class="flex flex-wrap gap-2 mb-3"></div>

            <div id="entity-list" class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-72 overflow-y-auto"></div>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
            <button onclick="previewTemplate()"
                    class="w-full bg-gray-900 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-black transition-all shadow-lg">
              👀 Preview
            </button>
            <button onclick="saveTemplate()"
                    class="w-full bg-gradient-to-r from-purple-600 to-blue-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-purple-700 hover:to-blue-700 transition-all shadow-lg">
              💾 Opslaan
            </button>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
            <button onclick="testTemplate()"
                    class="w-full bg-green-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-green-700 transition-all shadow-lg">
              ✅ Test (Jinja) in HA
            </button>
            <button onclick="yamlCheck()"
                    class="w-full bg-amber-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-amber-700 transition-all shadow-lg">
              🧰 YAML check
            </button>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-3">
            <button onclick="loadTemplates()"
                    class="w-full bg-gradient-to-r from-gray-600 to-gray-800 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-gray-700 hover:to-gray-900 transition-all shadow-lg">
              📋 Mijn Templates
            </button>
            <button onclick="downloadYaml()"
                    class="w-full bg-white border border-gray-300 text-gray-800 py-3 px-4 rounded-xl text-lg font-semibold hover:bg-gray-100 transition-all shadow-lg">
              ⬇️ Download YAML
            </button>
            <button onclick="generateAutomation()"
                    class="w-full bg-indigo-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-indigo-700 transition-all shadow-lg">
              🤖 Automation snippet
            </button>
          </div>
        </div>

        <div>
          <div class="bg-gray-50 p-6 rounded-xl border border-gray-200">
            <div class="flex items-center justify-between mb-3">
              <h3 class="text-xl font-bold text-gray-800">🧾 YAML</h3>
              <div class="flex gap-2">
                <button onclick="copyYaml()"
                        class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">📋 Copy</button>
                <button onclick="copyAutomation()"
                        class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">📋 Copy automation</button>
              </div>
            </div>
            <pre id="previewCode" class="bg-gray-900 text-green-400 p-4 rounded-lg overflow-x-auto text-sm font-mono min-h-[260px]"></pre>
          </div>

          <div class="mt-4 bg-white p-6 rounded-xl border border-gray-200">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-xl font-bold text-gray-800">🧪 Test output</h3>
              <span id="testBadge" class="text-xs px-2 py-1 rounded bg-gray-200 text-gray-700">Nog niet getest</span>
            </div>
            <pre id="testResult" class="bg-gray-50 p-4 rounded-lg overflow-x-auto text-sm font-mono min-h-[120px] text-gray-800"></pre>
          </div>

          <div class="mt-4 bg-white p-6 rounded-xl border border-gray-200">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-xl font-bold text-gray-800">🤖 Automation snippet</h3>
              <span class="text-xs px-2 py-1 rounded bg-gray-200 text-gray-700">Best-effort</span>
            </div>
            <pre id="automationCode" class="bg-gray-50 p-4 rounded-lg overflow-x-auto text-sm font-mono min-h-[120px] text-gray-800"></pre>
          </div>
        </div>
      </div>
    </div>

    <div id="templatesList" class="bg-white rounded-2xl shadow-2xl p-8 hidden">
      <h2 class="text-2xl font-bold text-gray-800 mb-4">📚 Opgeslagen Templates</h2>
      <div id="templatesContent" class="space-y-3"></div>
    </div>
  </div>

<script>
  let entities = [];
  let catalog = {{}};
  let selectedEntities = [];

  const API_BASE = window.location.pathname.replace(/\\/$/, '');

  function setStatus(text, color = 'gray') {{
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }}

  function escapeHtml(str) {{
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  async function init() {{
    setStatus('Verbinden...', 'yellow');

    const cfgRes = await fetch(API_BASE + '/api/config');
    const cfg = await cfgRes.json();
    if (!cfg.token_configured) document.getElementById('tokenWarning').classList.remove('hidden');

    const catRes = await fetch(API_BASE + '/api/catalog');
    catalog = await catRes.json();

    const typeSelect = document.getElementById('templateType');
    Object.keys(catalog).forEach(key => {{
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = catalog[key].title;
      typeSelect.appendChild(opt);
    }});

    const entRes = await fetch(API_BASE + '/api/entities');
    entities = await entRes.json();

    setStatus('Verbonden (' + entities.length + ' entities)', 'green');
    document.getElementById('previewCode').textContent = '# Kies een type en klik Preview.';
    document.getElementById('testResult').textContent = '—';
    document.getElementById('automationCode').textContent = '—';
  }}

  function renderSuggestions(typeKey) {{
    const box = document.getElementById('suggestionsBox');
    const list = document.getElementById('suggestionsList');
    list.innerHTML = '';
    const s = (catalog[typeKey] && catalog[typeKey].suggestions) || [];
    if (!s.length) {{ box.classList.add('hidden'); return; }}
    s.forEach(item => {{
      const li = document.createElement('li');
      li.textContent = item;
      list.appendChild(li);
    }});
    box.classList.remove('hidden');
  }}

  function renderParams(typeKey) {{
    const box = document.getElementById('paramsBox');
    const fields = document.getElementById('paramsFields');
    fields.innerHTML = '';
    const params = (catalog[typeKey] && catalog[typeKey].params) || [];
    if (!params.length) {{ box.classList.add('hidden'); return; }}

    params.forEach(p => {{
      const wrap = document.createElement('div');
      const label = document.createElement('label');
      label.className = 'block text-sm font-semibold text-gray-700 mb-1';
      label.textContent = p.label;

      let input;
      if (p.type === 'select') {{
        input = document.createElement('select');
        input.className = 'w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-purple-500 focus:outline-none';
        (p.options || []).forEach(optVal => {{
          const opt = document.createElement('option');
          opt.value = optVal;
          opt.textContent = optVal;
          input.appendChild(opt);
        }});
        if (p.default !== undefined) input.value = p.default;
      }} else {{
        input = document.createElement('input');
        input.type = (p.type === 'int' || p.type === 'float') ? 'number' : 'text';
        if (p.type === 'float') input.step = 'any';
        input.className = 'w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-purple-500 focus:outline-none';
        if (p.default !== undefined) input.value = p.default;
      }}

      input.id = 'param__' + p.key;
      wrap.appendChild(label);
      wrap.appendChild(input);
      fields.appendChild(wrap);
    }});

    box.classList.remove('hidden');
  }}

  function renderSelectedChips() {{
    const box = document.getElementById('selectedChips');
    box.innerHTML = '';
    selectedEntities.slice(0, 50).forEach(eid => {{
      const chip = document.createElement('div');
      chip.className = 'text-xs bg-purple-100 border border-purple-200 text-purple-900 px-2 py-1 rounded-full flex items-center gap-2';
      chip.innerHTML = '<span class="font-mono">' + escapeHtml(eid) + '</span>' +
                       '<button class="text-purple-700 hover:text-purple-900" title="remove">✕</button>';
      chip.querySelector('button').onclick = () => {{
        selectedEntities = selectedEntities.filter(x => x !== eid);
        renderEntities();
        renderSelectedChips();
      }};
      box.appendChild(chip);
    }});
  }}

  function renderEntities() {{
    const typeKey = document.getElementById('templateType').value;
    const needs = catalog[typeKey] && catalog[typeKey].needs_entities;

    const box = document.getElementById('entitiesBox');
    const list = document.getElementById('entity-list');
    const hint = document.getElementById('entitiesHint');
    list.innerHTML = '';

    if (!needs) {{ box.classList.add('hidden'); return; }}

    hint.textContent = (typeKey === 'last_changed_human' || typeKey === 'age_minutes')
      ? 'Selecteer precies 1 entity.'
      : 'Selecteer 1 of meer entities.';

    let filtered = entities;
    const domains = (catalog[typeKey] && catalog[typeKey].entity_filter && catalog[typeKey].entity_filter.domains) || [];
    if (domains.length) filtered = filtered.filter(e => domains.includes(e.domain));

    const q = (document.getElementById('entitySearch').value || '').toLowerCase().trim();
    if (q) {{
      filtered = filtered.filter(e => (String(e.name||'').toLowerCase().includes(q) || String(e.entity_id||'').toLowerCase().includes(q)));
    }}

    filtered.forEach(e_attach => {{
      const div = document.createElement('div');
      div.className = 'entity-select p-3 border-2 border-gray-200 rounded-lg cursor-pointer hover:bg-purple-50 hover:border-purple-300 transition-all';
      if (selectedEntities.includes(e_attach.entity_id)) {{
        div.classList.add('bg-purple-100','border-purple-500');
        div.classList.remove('border-gray-200');
      }}
      div.innerHTML =
        '<div class="font-semibold text-sm">' + escapeHtml(e_attach.name) + '</div>' +
        '<div class="text-xs text-gray-500 font-mono">' + escapeHtml(e_attach.entity_id) + '</div>';
      div.onclick = () => toggleEntity(div, e_attach.entity_id);
      list.appendChild(div);
    }});

    box.classList.remove('hidden');
  }}

  function onTypeChange() {{
    const typeKey = document.getElementById('templateType').value;

    renderSuggestions(typeKey);
    renderParams(typeKey);

    const iconInput = document.getElementById('templateIcon');
    if ((!iconInput.value || !iconInput.value.trim()) && catalog[typeKey] && catalog[typeKey].defaults && catalog[typeKey].defaults.icon) {{
      iconInput.value = catalog[typeKey].defaults.icon;
    }}

    selectedEntities = [];
    renderSelectedChips();
    document.getElementById('entitySearch').value = '';
    renderEntities();
  }}

  function toggleEntity(el, entityId) {{
    const i = selectedEntities.indexOf(entityId);
    if (i > -1) {{
      selectedEntities.splice(i, 1);
      el.classList.remove('bg-purple-100','border-purple-500');
      el.classList.add('border-gray-200');
    }} else {{
      selectedEntities.push(entityId);
      el.classList.add('bg-purple-100','border-purple-500');
      el.classList.remove('border-gray-200');
    }}
    renderSelectedChips();
  }}

  function collectParams(typeKey) {{
    const params = (catalog[typeKey] && catalog[typeKey].params) || [];
    const out = {{}};
    params.forEach(p => {{
      const el = document.getElementById('param__' + p.key);
      if (!el) return;
      let v = el.value;
      if (p.type === 'int') v = parseInt(v || '0', 10);
      if (p.type === 'float') v = parseFloat(v || '0');
      out[p.key] = v;
    }});
    return out;
  }}

  function currentPayload() {{
    const name = document.getElementById('templateName').value.trim();
    const type = document.getElementById('templateType').value;
    const icon = document.getElementById('templateIcon').value.trim();
    const params = collectParams(type);

    const single_file = document.getElementById('singleFileMode').checked;
    const overwrite = document.getElementById('overwriteMode').checked;
    const auto_suffix = document.getElementById('autoSuffixMode').checked;

    return {{ name, type, icon, entities: selectedEntities, params, single_file, overwrite, auto_suffix }};
  }}

  async function previewTemplate() {{
    const p = currentPayload();
    if (!p.name) return alert('❌ Vul een naam in!');
    if (!p.type) return alert('❌ Kies een template type!');

    const res = await fetch(API_BASE + '/api/preview_template', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(p)
    }});
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Onbekende fout'));
    document.getElementById('previewCode').textContent = data.code;
  }}

  async function saveTemplate() {{
    const p = currentPayload();
    if (!p.name) return alert('❌ Vul een naam in!');
    if (!p.type) return alert('❌ Kies een template type!');

    const res = await fetch(API_BASE + '/api/create_template', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(p)
    }});
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Onbekende fout'));
    document.getElementById('previewCode').textContent = data.code;
    alert('✅ Opgeslagen als ' + data.filename + '\\n\\nTip: Reload Template Entities in HA.');
  }}

  async function testTemplate() {{
    const p = currentPayload();
    if (!p.name) return alert('❌ Vul een naam in!');
    if (!p.type) return alert('❌ Kies een template type!');

    const res = await fetch(API_BASE + '/api/test_template', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(p)
    }});
    const data = await res.json();

    const badge = document.getElementById('testBadge');
    const out = document.getElementById('testResult');

    if (!res.ok || !data.ok) {{
      badge.className = 'text-xs px-2 py-1 rounded bg-red-100 text-red-700';
      badge.textContent = 'Test mislukt';
      out.textContent = (data.error || 'Onbekende fout') + (data.details ? ('\\n\\n' + data.details) : '');
      return;
    }}

    badge.className = 'text-xs px-2 py-1 rounded bg-green-100 text-green-700';
    badge.textContent = 'OK';
    out.textContent = data.result;
  }}

  async function yamlCheck() {{
    const p = currentPayload();
    if (!p.name) return alert('❌ Vul een naam in!');
    if (!p.type) return alert('❌ Kies een template type!');

    const res = await fetch(API_BASE + '/api/yaml_check', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(p)
    }});
    const data = await res.json();

    const badge = document.getElementById('testBadge');
    const out = document.getElementById('testResult');

    if (!res.ok || !data.ok) {{
      badge.className = 'text-xs px-2 py-1 rounded bg-red-100 text-red-700';
      badge.textContent = 'YAML check fail';
      out.textContent = (data.error || 'Onbekende fout') + (data.details ? ('\\n\\n' + data.details) : '');
      return;
    }}

    badge.className = 'text-xs px-2 py-1 rounded bg-green-100 text-green-700';
    badge.textContent = 'YAML OK';
    out.textContent = data.result || 'OK';
  }}

  async function reloadTemplatesInHA() {{
    const res = await fetch(API_BASE + '/api/reload_templates', {{ method: 'POST' }});
    const data = await res.json();
    if (!res.ok || !data.ok) {{
      return alert('❌ Reload failed: ' + (data.error || 'Onbekend') + (data.details ? ('\\n\\n' + JSON.stringify(data.details)) : ''));
    }}
    alert('✅ Reload request verstuurd: ' + (data.result || 'OK'));
  }}

  async function loadTemplates() {{
    const response = await fetch(API_BASE + '/api/templates');
    const templates = await response.json();

    const list = document.getElementById('templatesList');
    const content = document.getElementById('templatesContent');

    if (!templates.length) {{
      list.classList.add('hidden');
      return alert('Nog geen templates aangemaakt!');
    }}

    list.classList.remove('hidden');

    let html = '';
    templates.forEach(t => {{
      html += '<div class="bg-gray-50 border-2 border-gray-200 rounded-lg p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">';
      html += '<div><div class="font-semibold">' + escapeHtml(t.name) + '</div>';
      html += '<div class="text-sm text-gray-500 font-mono">' + escapeHtml(t.filename) + '</div></div>';
      html += '<div class="flex gap-2 flex-wrap">';
      html += '<button onclick="openTemplate(\\'' + t.filename + '\\')" class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700">📄 Open</button>';
      html += '<button onclick="downloadExisting(\\'' + t.filename + '\\')" class="bg-white border border-gray-300 text-gray-800 px-4 py-2 rounded-lg hover:bg-gray-100">⬇️ Download</button>';
      html += '<button onclick="deleteTemplate(\\'' + t.filename + '\\')" class="bg-red-500 text-white px-4 py-2 rounded-lg hover:bg-red-600">🗑️ Verwijder</button>';
      html += '</div></div>';
    }});

    content.innerHTML = html;
    list.scrollIntoView({{ behavior: 'smooth' }});
  }}

  async function openTemplate(filename) {{
    const res = await fetch(API_BASE + '/api/template?filename=' + encodeURIComponent(filename));
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Kon template niet openen'));
    document.getElementById('previewCode').textContent = data.code;
    document.getElementById('templateName').value = data.name_guess || '';
  }}

  async function deleteTemplate(filename) {{
    if (!confirm('Weet je zeker dat je ' + filename + ' wilt verwijderen?')) return;
    const response = await fetch(API_BASE + '/api/delete_template', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ filename }})
    }});
    const result = await response.json();
    if (response.ok) {{
      alert('✅ Template verwijderd!');
      loadTemplates();
    }} else {{
      alert('❌ Fout: ' + (result.error || 'Onbekende fout'));
    }}
  }}

  function copyYaml() {{
    const text = document.getElementById('previewCode').textContent || '';
    navigator.clipboard.writeText(text).then(() => alert('📋 YAML gekopieerd!'));
  }}

  async function downloadYaml() {{
    const code = document.getElementById('previewCode').textContent || '';
    if (!code || code.startsWith('# Kies')) return alert('Geen YAML om te downloaden. Maak eerst een preview.');
    const blob = new Blob([code], {{ type: 'text/yaml' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'template.yaml';
    a.click();
    URL.revokeObjectURL(url);
  }}

  async function downloadExisting(filename) {{
    window.open(API_BASE + '/api/download?filename=' + encodeURIComponent(filename), '_blank');
  }}

  function selectAll() {{
    const list = document.getElementById('entity-list');
    const cards = list.querySelectorAll('.entity-select');
    cards.forEach(card => {{
      const eid = card.querySelector('.font-mono')?.textContent || '';
      if (eid && !selectedEntities.includes(eid)) selectedEntities.push(eid);
    }});
    renderEntities();
    renderSelectedChips();
  }}

  function clearAll() {{
    selectedEntities = [];
    renderEntities();
    renderSelectedChips();
  }}

  async function generateAutomation() {{
    const p = currentPayload();
    if (!p.name) return alert('❌ Vul een naam in!');
    if (!p.type) return alert('❌ Kies een template type!');
    const res = await fetch(API_BASE + '/api/automation_snippet', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(p)
    }});
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Onbekende fout'));
    document.getElementById('automationCode').textContent = data.code || '—';
  }}

  init();
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# -------------------------
# API routes
# -------------------------

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "token_configured": bool(SUPERVISOR_TOKEN),
        "templates_path": TEMPLATES_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "token_debug": {
            "env_SUPERVISOR_TOKEN": bool((os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()),
            "env_HOMEASSISTANT_TOKEN": bool((os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()),
            "file_supervisor_token": bool(_read_file("/var/run/supervisor_token")),
        }
    })

@app.route("/api/catalog", methods=["GET"])
def get_catalog():
    meta = {}
    for k, v in TEMPLATE_CATALOG.items():
        meta[k] = {
            "title": v.get("title"),
            "needs_entities": bool(v.get("needs_entities")),
            "params": v.get("params", []),
            "defaults": v.get("defaults", {}),
            "suggestions": v.get("suggestions", []),
            "kind": v.get("kind", ""),
            "entity_filter": v.get("entity_filter", {"domains": []}),
        }
    return jsonify(meta)

@app.route("/api/entities", methods=["GET"])
def api_entities():
    # If no token, return helpful minimal demo so UI still works
    if not SUPERVISOR_TOKEN:
        return jsonify([
            {"entity_id": "light.woonkamer", "domain": "light", "name": "Woonkamer Lamp"},
            {"entity_id": "sensor.temp_woonkamer", "domain": "sensor", "name": "Temp Woonkamer"},
            {"entity_id": "binary_sensor.deur_voordeur", "domain": "binary_sensor", "name": "Voordeur"},
        ])

    states, err, status = ha_get_states()
    if err:
        # still return empty but include log in console
        print(f"Entities error: {err} ({status})")
        return jsonify([])

    entities = []
    for s in states or []:
        entity_id = s.get("entity_id", "")
        if not entity_id:
            continue
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        attrs = s.get("attributes") or {}
        friendly = attrs.get("friendly_name", entity_id)
        entities.append({"entity_id": entity_id, "domain": domain, "name": friendly})
    return jsonify(entities)

@app.route("/api/templates", methods=["GET"])
def api_templates():
    files = list_yaml_files(TEMPLATES_PATH)
    return jsonify([{"filename": fn, "name": fn.replace(".yaml", "").replace("_", " ").title()} for fn in files])

@app.route("/api/template", methods=["GET"])
def read_template():
    filename = (request.args.get("filename", "") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(TEMPLATES_PATH, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Bestand niet gevonden"}), 404
    content = read_text_file(filepath)
    name_guess = filename.replace(".yaml", "").replace("_", " ").title()
    return jsonify({"filename": filename, "code": content, "name_guess": name_guess})

@app.route("/api/download", methods=["GET"])
def download_template():
    filename = (request.args.get("filename", "") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(TEMPLATES_PATH, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Bestand niet gevonden"}), 404
    content = read_text_file(filepath)
    return Response(content, mimetype="text/yaml",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/preview_template", methods=["POST"])
def preview_template():
    data = request.json or {}
    template_type = (data.get("type") or "").strip()
    name = (data.get("name") or "Nieuwe Sensor").strip()
    icon = (data.get("icon") or "").strip()
    params = data.get("params") or {}
    selected_entities = data.get("entities") or []

    safe_name = sanitize_filename(name)
    cfg, err = build_template_config(template_type, name, safe_name, icon, selected_entities, params)
    if err:
        return jsonify({"error": err}), 400

    code = safe_yaml_dump(cfg)
    return jsonify({"ok": True, "code": code})

@app.route("/api/create_template", methods=["POST"])
def create_template():
    data = request.json or {}
    template_type = (data.get("type") or "").strip()
    name = (data.get("name") or "Nieuwe Sensor").strip()
    icon = (data.get("icon") or "").strip()
    params = data.get("params") or {}
    selected_entities = data.get("entities") or []

    single_file = bool(data.get("single_file", False))
    overwrite = bool(data.get("overwrite", False))
    auto_suffix = bool(data.get("auto_suffix", True))

    safe_name = sanitize_filename(name)
    cfg, err = build_template_config(template_type, name, safe_name, icon, selected_entities, params)
    if err:
        return jsonify({"error": err}), 400

    code = safe_yaml_dump(cfg)

    if single_file:
        filename = "template_maker.yaml"
        filepath = os.path.join(TEMPLATES_PATH, filename)

        header = f"\n# ---- {name} ({template_type}) ----\n"
        if not os.path.exists(filepath):
            write_text_file(filepath, "# Generated by Template Maker Pro\n")

        existing = read_text_file(filepath)

        if overwrite:
            pattern = re.compile(rf"(?ms)^# ---- {re.escape(name)} \({re.escape(template_type)}\) ----\n.*?(?=^# ---- |\Z)")
            existing = pattern.sub("", existing)

        combined = existing.rstrip() + "\n" + header + code.strip() + "\n"
        write_text_file(filepath, combined)
        return jsonify({"success": True, "filename": filename, "code": combined, "message": f"Toegevoegd aan {filename}"})

    desired = f"{safe_name}.yaml"
    filepath = os.path.join(TEMPLATES_PATH, desired)

    if os.path.exists(filepath) and not overwrite:
        if auto_suffix:
            desired = next_available_filename(TEMPLATES_PATH, desired)
            filepath = os.path.join(TEMPLATES_PATH, desired)
        else:
            return jsonify({"error": f"Bestand bestaat al: {desired}. Zet 'overschrijven' aan of 'auto suffix'."}), 400

    write_text_file(filepath, code)
    return jsonify({"success": True, "filename": desired, "code": code, "message": f"Template opgeslagen als {desired}"})

@app.route("/api/delete_template", methods=["POST"])
def delete_template():
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(TEMPLATES_PATH, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({"success": True, "message": f"{filename} verwijderd"})
    return jsonify({"error": "Bestand niet gevonden"}), 404

@app.route("/api/test_template", methods=["POST"])
def test_template():
    data = request.json or {}
    template_type = (data.get("type") or "").strip()
    name = (data.get("name") or "Nieuwe Sensor").strip()
    icon = (data.get("icon") or "").strip()
    params = data.get("params") or {}
    selected_entities = data.get("entities") or []

    safe_name = sanitize_filename(name)
    cfg, err = build_template_config(template_type, name, safe_name, icon, selected_entities, params)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    st = extract_first_state_template(cfg)
    if not st:
        return jsonify({"ok": False, "error": "Kon geen 'state' template vinden om te testen."}), 400

    result, status = ha_template_render(st, variables={})
    return jsonify(result), status

@app.route("/api/yaml_check", methods=["POST"])
def yaml_check():
    data = request.json or {}
    template_type = (data.get("type") or "").strip()
    name = (data.get("name") or "Nieuwe Sensor").strip()
    icon = (data.get("icon") or "").strip()
    params = data.get("params") or {}
    selected_entities = data.get("entities") or []

    safe_name = sanitize_filename(name)
    cfg, err = build_template_config(template_type, name, safe_name, icon, selected_entities, params)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    code = safe_yaml_dump(cfg)

    # local YAML parse
    try:
        yaml.safe_load(code)
    except Exception as e:
        return jsonify({"ok": False, "error": "YAML parse error", "details": str(e)}), 400

    # Optional Jinja render test
    st = extract_first_state_template(cfg)
    if st and SUPERVISOR_TOKEN:
        r, _ = ha_template_render(st, variables={})
        if r.get("ok"):
            return jsonify({"ok": True, "result": "YAML parse OK + Jinja render OK."}), 200
        return jsonify({"ok": False, "error": "Jinja render failed", "details": (r.get("error") or "") + ("\n" + r.get("details","") if r.get("details") else "")}), 400

    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": True, "result": "YAML parse OK (offline)."}), 200

    return jsonify({"ok": True, "result": "YAML parse OK."}), 200

@app.route("/api/reload_templates", methods=["POST"])
def reload_templates():
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "Geen token in container; kan niet reloaden."}), 400

    # Most common in modern HA
    candidates = [
        ("template", "reload", {}),
        ("homeassistant", "reload_core_config", {}),
    ]

    last = None
    for domain, service, payload in candidates:
        r, status = ha_call_service(domain, service, payload)
        if status == 200 and r.get("ok"):
            return jsonify({"ok": True, "result": f"{domain}.{service}"}), 200
        last = r

    return jsonify({"ok": False, "error": "Geen werkende reload service gevonden.", "details": last}), 400

@app.route("/api/automation_snippet", methods=["POST"])
def automation_snippet():
    data = request.json or {}
    template_type = (data.get("type") or "").strip()
    name = (data.get("name") or "Nieuwe Sensor").strip()
    icon = (data.get("icon") or "").strip()
    params = data.get("params") or {}
    selected_entities = data.get("entities") or []

    safe_name = sanitize_filename(name)
    cfg, err = build_template_config(template_type, name, safe_name, icon, selected_entities, params)
    if err:
        return jsonify({"error": err}), 400

    kind, uid = extract_entity_info(cfg)
    if not kind or not uid:
        return jsonify({"error": "Kon unique_id/kind niet bepalen voor snippet."}), 400

    trigger_mode = "state"
    threshold = None
    if kind == "sensor" and template_type in ("sum_power", "sum_energy", "average_temp"):
        trigger_mode = "numeric_state"
        threshold = 0

    snippet = build_automation_snippet(name, uid, kind, trigger_mode, threshold)
    return jsonify({"ok": True, "code": safe_yaml_dump(snippet)})

# -------------------------
# main
# -------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"{APP_NAME} starting... ({APP_VERSION})")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8099, debug=False)
