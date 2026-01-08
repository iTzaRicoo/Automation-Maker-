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

APP_VERSION = "1.2.1-beta-fix-tests"
APP_NAME = "Template Maker Pro"

app = Flask(__name__)

HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
TEMPLATES_PATH = os.environ.get("TEMPLATES_PATH") or os.path.join(HA_CONFIG_PATH, "include", "templates")

# -------------------------
# Token discovery (HAOS add-on)
# -------------------------
def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""

def discover_token() -> str:
    # Preferred for HAOS add-ons
    tok = (os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()
    if tok:
        return tok

    # Some environments
    tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()
    if tok:
        return tok

    # Common paths inside add-on containers
    for p in ("/var/run/supervisor_token", "/run/supervisor_token"):
        tok = _read_file(p)
        if tok:
            return tok

    return ""

SUPERVISOR_TOKEN = discover_token()

Path(TEMPLATES_PATH).mkdir(parents=True, exist_ok=True)

print(f"== {APP_NAME} {APP_VERSION} ==")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Templates path: {TEMPLATES_PATH}")
print(f"Token available: {bool(SUPERVISOR_TOKEN)}")
print(f"Token debug: env_SUPERVISOR_TOKEN={bool((os.environ.get('SUPERVISOR_TOKEN','') or '').strip())}, "
      f"env_HOMEASSISTANT_TOKEN={bool((os.environ.get('HOMEASSISTANT_TOKEN','') or '').strip())}, "
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
# Home Assistant API (Supervisor proxy)
# -------------------------
def ha_request(method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
    url = f"http://supervisor/core{path}"
    return requests.request(method, url, headers=ha_headers(), json=json_body, timeout=timeout)

def ha_get_states() -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], int]:
    if not SUPERVISOR_TOKEN:
        return None, "Geen token in container (SUPERVISOR_TOKEN/HOMEASSISTANT_TOKEN of supervisor_token file).", 400
    try:
        resp = ha_request("GET", "/api/states", timeout=12)
        if resp.status_code != 200:
            return None, f"HA states fetch failed: {resp.status_code}", resp.status_code
        return resp.json(), None, 200
    except Exception as e:
        return None, str(e), 500

def ha_template_render(template_str: str, variables: dict | None = None) -> Tuple[Dict[str, Any], int]:
    """
    Render via /api/template.
    IMPORTANT: /api/template expects a Jinja template string. It supports {{ ... }} and usually also {% ... %},
    but we handle fallbacks + return detailed errors.
    """
    if not SUPERVISOR_TOKEN:
        return {"ok": False, "error": "Geen token in container; kan niet testen tegen Home Assistant."}, 400

    payload = {"template": template_str}
    if variables:
        payload["variables"] = variables

    try:
        resp = ha_request("POST", "/api/template", json_body=payload, timeout=15)
        if resp.status_code != 200:
            return {
                "ok": False,
                "error": f"HA template render failed: {resp.status_code}",
                "details": resp.text[:2000]
            }, 400
        return {"ok": True, "result": resp.text}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

def ha_call_service(domain: str, service: str, data: dict | None = None) -> Tuple[Dict[str, Any], int]:
    if not SUPERVISOR_TOKEN:
        return {"ok": False, "error": "Geen token in container; kan geen service call doen."}, 400
    try:
        resp = ha_request("POST", f"/api/services/{domain}/{service}", json_body=(data or {}), timeout=15)
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"Service call failed: {resp.status_code}", "details": resp.text[:2000]}, 400
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

def build_last_changed_human(entity_id: str) -> str:
    # This contains {% %} -> works in HA template engine, but sometimes users hit edge cases.
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

# Catalog (meest gangbaar)
add_template("count_lights", {
    "title": "💡 Tel lampen aan",
    "kind": "sensor",
    "needs_entities": False,
    "params": [],
    "defaults": {"icon": "mdi:lightbulb-group"},
    "suggestions": ["Dashboard: laat een badge rood worden als > 0."],
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
    "suggestions": ["Selecteer power sensoren (W). Unknown/unavailable wordt genegeerd."],
    "entity_filter": {"domains": ["sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        "{{ " + entities_to_jinja_list(entities or []) + " | map('states') | reject('in',['unknown','unavailable']) | map('float', 0) | sum | round(" + str(int(p.get("round", 2))) + ") }}",
        "mdi:flash",
        {"unit_of_measurement": "W", "device_class": "power"}
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
    "suggestions": ["True als één entity 'on' of 'open' is."],
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
    "suggestions": ["Gebruik voor power > 2000W, humidity > 60%, etc."],
    "entity_filter": {"domains": ["sensor"]},
    "builder": lambda name, uid, p, entities=None: basic_binary(
        name, uid,
        build_threshold_state(entities or [], float(p.get("threshold", 100)), p.get("mode", "any")),
        "mdi:alert"
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

add_template("unavailable_count_domain", {
    "title": "🚫 Aantal unavailable/unknown (per domein)",
    "kind": "sensor",
    "needs_entities": False,
    "params": [{"key": "domain", "label": "Domein", "type": "select",
                "options": ["sensor", "light", "switch", "binary_sensor", "climate", "media_player"], "default": "sensor"}],
    "defaults": {"icon": "mdi:alert-circle"},
    "suggestions": ["Handig voor snel integratie-issues spotten."],
    "entity_filter": {"domains": []},
    "builder": lambda name, uid, p, entities=None: basic_sensor(
        name, uid,
        build_unavailable_count(p.get("domain", "sensor")),
        "mdi:alert-circle"
    ),
})

# -------------------------
# Build logic
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
            return None, "Deze template heeft entities nodig."
        if template_type in ("last_changed_human", "age_minutes") and len(entities) != 1:
            return None, "Selecteer precies 1 entity voor dit type."

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

def validate_generated_config(cfg: dict) -> Tuple[bool, str]:
    """
    Sanity validation so /test and /yaml_check return helpful errors.
    """
    if not isinstance(cfg, dict) or "template" not in cfg:
        return False, "Top-level moet 'template:' bevatten."
    if not isinstance(cfg["template"], list) or not cfg["template"]:
        return False, "'template' moet een lijst zijn met minimaal 1 item."

    block = cfg["template"][0]
    if not isinstance(block, dict):
        return False, "Eerste template block is geen dict."

    if "sensor" in block:
        items = block["sensor"]
        if not items or not isinstance(items, list):
            return False, "sensor block mist lijst."
        if not items[0].get("name") or not items[0].get("state"):
            return False, "sensor mist 'name' of 'state'."
    elif "binary_sensor" in block:
        items = block["binary_sensor"]
        if not items or not isinstance(items, list):
            return False, "binary_sensor block mist lijst."
        if not items[0].get("name") or not items[0].get("state"):
            return False, "binary_sensor mist 'name' of 'state'."
    else:
        return False, "Block moet 'sensor' of 'binary_sensor' bevatten."

    return True, "OK"

# -------------------------
# ROUTES
# -------------------------
@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "token_configured": bool(SUPERVISOR_TOKEN),
        "templates_path": TEMPLATES_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    })

@app.route("/api/catalog", methods=["GET"])
def api_catalog():
    out = {}
    for k, v in TEMPLATE_CATALOG.items():
        out[k] = {
            "title": v.get("title"),
            "needs_entities": bool(v.get("needs_entities")),
            "params": v.get("params", []),
            "defaults": v.get("defaults", {}),
            "suggestions": v.get("suggestions", []),
            "kind": v.get("kind", ""),
            "entity_filter": v.get("entity_filter", {"domains": []}),
        }
    return jsonify(out)

@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    """
    Debug endpoint to see why HA calls fail.
    """
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "No token in container."}), 200

    # test call
    try:
        r = ha_request("GET", "/api/", timeout=10)
        return jsonify({"ok": True, "status": r.status_code, "body": r.text[:300]}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

@app.route("/api/entities", methods=["GET"])
def api_entities():
    if not SUPERVISOR_TOKEN:
        # demo to keep UI working
        return jsonify([
            {"entity_id": "light.woonkamer", "domain": "light", "name": "Woonkamer Lamp"},
            {"entity_id": "sensor.temp_woonkamer", "domain": "sensor", "name": "Temp Woonkamer"},
            {"entity_id": "binary_sensor.deur_voordeur", "domain": "binary_sensor", "name": "Voordeur"},
        ])

    states, err, status = ha_get_states()
    if err:
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
def api_template_read():
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
def api_download():
    filename = (request.args.get("filename", "") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(TEMPLATES_PATH, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Bestand niet gevonden"}), 404
    content = read_text_file(filepath)
    return Response(content, mimetype="text/yaml", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/api/preview_template", methods=["POST"])
def api_preview():
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

    ok, msg = validate_generated_config(cfg)
    if not ok:
        return jsonify({"error": msg}), 400

    return jsonify({"ok": True, "code": safe_yaml_dump(cfg)})

@app.route("/api/create_template", methods=["POST"])
def api_create():
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

    ok, msg = validate_generated_config(cfg)
    if not ok:
        return jsonify({"error": msg}), 400

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
        return jsonify({"success": True, "filename": filename, "code": combined})

    desired = f"{safe_name}.yaml"
    filepath = os.path.join(TEMPLATES_PATH, desired)

    if os.path.exists(filepath) and not overwrite:
        if auto_suffix:
            desired = next_available_filename(TEMPLATES_PATH, desired)
            filepath = os.path.join(TEMPLATES_PATH, desired)
        else:
            return jsonify({"error": f"Bestand bestaat al: {desired}"}), 400

    write_text_file(filepath, code)
    return jsonify({"success": True, "filename": desired, "code": code})

@app.route("/api/delete_template", methods=["POST"])
def api_delete():
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(TEMPLATES_PATH, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({"success": True})
    return jsonify({"error": "Bestand niet gevonden"}), 404

@app.route("/api/test_template", methods=["POST"])
def api_test_template():
    """
    Robust tester: returns full HA error details.
    """
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

    ok, msg = validate_generated_config(cfg)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    state_tpl = extract_first_state_template(cfg)
    if not state_tpl:
        return jsonify({"ok": False, "error": "Geen state template gevonden."}), 400

    # Attempt direct render
    r, status = ha_template_render(state_tpl, variables={})
    if r.get("ok"):
        return jsonify(r), 200

    # Fallback: If template contains {% %}, HA might still support it,
    # but if it fails, we provide a clearer diagnostic
    return jsonify(r), status

@app.route("/api/yaml_check", methods=["POST"])
def api_yaml_check():
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

    ok, msg = validate_generated_config(cfg)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    code = safe_yaml_dump(cfg)

    # YAML parse check (local)
    try:
        yaml.safe_load(code)
    except Exception as e:
        return jsonify({"ok": False, "error": "YAML parse error", "details": str(e)}), 400

    # Optional: Jinja render check
    state_tpl = extract_first_state_template(cfg)
    if state_tpl and SUPERVISOR_TOKEN:
        r, _ = ha_template_render(state_tpl, variables={})
        if r.get("ok"):
            return jsonify({"ok": True, "result": "YAML parse OK + Jinja render OK."}), 200
        return jsonify({"ok": False, "error": "Jinja render failed", "details": r.get("details") or r.get("error")}), 400

    return jsonify({"ok": True, "result": "YAML parse OK."}), 200

@app.route("/api/reload_templates", methods=["POST"])
def api_reload_templates():
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "Geen token in container."}), 400

    # Most common service for template reload
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
def api_automation_snippet():
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
        return jsonify({"error": "Kon unique_id/kind niet bepalen."}), 400

    trigger_mode = "state"
    threshold = None
    if kind == "sensor" and template_type in ("sum_power", "average_temp"):
        trigger_mode = "numeric_state"
        threshold = 0

    snippet = {
        "alias": f"Reageer op {name}",
        "mode": "single",
        "trigger": [{"platform": "numeric_state" if trigger_mode == "numeric_state" else "state",
                     "entity_id": f"{kind}.{uid.replace('template_', '')}",
                     **({"above": threshold} if trigger_mode == "numeric_state" else {})}],
        "action": [{"service": "persistent_notification.create",
                    "data": {"title": "Template Maker", "message": f"{name} triggerde de automation."}}]
    }

    return jsonify({"ok": True, "code": safe_yaml_dump(snippet)})

# UI route: keep your existing UI if you want; for now just show minimal redirect hint.
@app.route("/")
def root():
    return ("<h2>Template Maker Pro draait.</h2><p>Open de web UI route die jij al had, of integreer je HTML opnieuw.</p>", 200)

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"{APP_NAME} starting... ({APP_VERSION})")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8099, debug=False)
