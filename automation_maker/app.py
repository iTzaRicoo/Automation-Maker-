#!/usr/bin/env python3
"""
Automation Maker - Home Assistant Add-on backend (Flask)

Goals:
- Simple REST API for UI (create/update/delete/list/test automations)
- Plays nice with Home Assistant Ingress (including weird /api/hassio_ingress/... paths)
- Beginner-friendly test steps (human text) + optional tech details in `extra.tech`
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
import yaml
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS


# -----------------------------------------------------------------------------
# App + Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
AUTOMATIONS_PATH = os.environ.get("AUTOMATIONS_PATH") or os.path.join(HA_CONFIG_PATH, "include", "automations")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

Path(AUTOMATIONS_PATH).mkdir(parents=True, exist_ok=True)

print(f"[Automation Maker] Config path: {HA_CONFIG_PATH}")
print(f"[Automation Maker] Automations path: {AUTOMATIONS_PATH}")
print(f"[Automation Maker] Supervisor token available: {bool(SUPERVISOR_TOKEN)}")


# -----------------------------------------------------------------------------
# Small utilities
# -----------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """Convert an automation name to a safe filename."""
    name = (name or "").strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "_", name)
    if not name:
        name = "unnamed"
    return name[:80]


def safe_join(base: str, filename: str) -> str:
    """Prevent directory traversal: only allow basename within base."""
    return os.path.join(base, os.path.basename(filename))


def ingress_path() -> str:
    """
    Home Assistant Ingress can send this header:
    - X-Ingress-Path: /api/hassio_ingress/<token>
    Not always present (non-ingress access, dev, etc).
    """
    return request.headers.get("X-Ingress-Path", "")


def ha_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}


def ha_call_service(domain: str, service: str, payload: Dict[str, Any]) -> Tuple[int, str]:
    """Call a Home Assistant service via Supervisor -> Core API."""
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("Geen Supervisor token beschikbaar (SUPERVISOR_TOKEN ontbreekt).")

    url = f"http://supervisor/core/api/services/{domain}/{service}"
    resp = requests.post(url, headers=ha_headers(), json=payload, timeout=15)
    return resp.status_code, resp.text


def reload_automations() -> bool:
    """Ask Home Assistant to reload automations. Safe to fail."""
    if not SUPERVISOR_TOKEN:
        print("[Automation Maker] No supervisor token, skipping automation reload")
        return False

    try:
        resp = requests.post(
            "http://supervisor/core/api/services/automation/reload",
            headers=ha_headers(),
            timeout=10,
        )
        ok = resp.status_code == 200
        print(f"[Automation Maker] Automations reload: {resp.status_code} ({'OK' if ok else 'FAIL'})")
        return ok
    except Exception as e:
        print(f"[Automation Maker] Error reloading automations: {e}")
        return False


# -----------------------------------------------------------------------------
# Home Assistant entities
# -----------------------------------------------------------------------------
def get_ha_entities() -> List[Dict[str, str]]:
    """Fetch all entity states and convert to a simple list for UI."""
    if not SUPERVISOR_TOKEN:
        print("[Automation Maker] No supervisor token, cannot fetch entities")
        return []

    try:
        resp = requests.get("http://supervisor/core/api/states", headers=ha_headers(), timeout=10)
        if resp.status_code != 200:
            print(f"[Automation Maker] Failed to fetch entities: {resp.status_code} {resp.text}")
            return []

        states = resp.json()
        entities: List[Dict[str, str]] = []
        for s in states:
            entity_id = s.get("entity_id", "")
            if not entity_id:
                continue
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            friendly = (s.get("attributes") or {}).get("friendly_name", entity_id)
            entities.append({"entity_id": entity_id, "domain": domain, "name": friendly})

        # Nice UX: sort by friendly name
        entities.sort(key=lambda x: (x.get("name") or "").lower())
        return entities
    except Exception as e:
        print(f"[Automation Maker] Error getting entities: {e}")
        return []


# -----------------------------------------------------------------------------
# YAML <-> UI model conversion
# -----------------------------------------------------------------------------
def parse_trigger_from_yaml(trigger_list: Any) -> Dict[str, Any]:
    if not trigger_list or not isinstance(trigger_list, list):
        return {"type": "", "value": ""}

    t = trigger_list[0] or {}
    platform = t.get("platform", "")

    if platform == "time":
        return {"type": "time", "value": t.get("at", "")}

    if platform == "state":
        model: Dict[str, Any] = {"type": "state", "value": t.get("entity_id", "")}
        if "to" in t:
            model["to"] = t.get("to")
        return model

    if platform == "sun":
        model = {"type": "sun", "sunEvent": t.get("event", "sunrise")}
        offset = t.get("offset")
        if offset:
            model["sunOffset"] = "before" if str(offset).startswith("-") else "after"
            parts = str(offset).replace("+", "").replace("-", "").split(":")
            if len(parts) >= 2 and parts[1].isdigit():
                model["sunMinutes"] = str(int(parts[1]))
            else:
                model["sunMinutes"] = "0"
        else:
            model["sunOffset"] = "after"
            model["sunMinutes"] = "0"
        return model

    # Unknown platform fallback
    return {"type": platform, "value": ""}


def parse_action_from_yaml(action_list: Any) -> Dict[str, Any]:
    if not action_list or not isinstance(action_list, list):
        return {"type": "", "value": ""}

    a = action_list[0] or {}
    service = a.get("service", "")

    # Light on/off with color/brightness
    if service in ("light.turn_on", "light.turn_off"):
        entity_id = ((a.get("target") or {}).get("entity_id")) or ""
        model: Dict[str, Any] = {
            "type": "turn_on" if service.endswith(".turn_on") else "turn_off",
            "value": entity_id,
        }
        data = a.get("data") or {}
        if "rgb_color" in data and isinstance(data["rgb_color"], list) and len(data["rgb_color"]) == 3:
            model["color_rgb"] = data["rgb_color"]
        if "brightness" in data:
            model["brightness"] = data["brightness"]
        return model

    # generic on/off
    if service in ("homeassistant.turn_on", "homeassistant.turn_off"):
        return {
            "type": "turn_on" if service.endswith(".turn_on") else "turn_off",
            "value": ((a.get("target") or {}).get("entity_id")) or "",
        }

    if service.startswith("notify."):
        msg = ((a.get("data") or {}).get("message")) or ""
        return {"type": "notify", "value": msg, "service": service}

    if service == "scene.turn_on":
        eid = ((a.get("target") or {}).get("entity_id")) or ""
        return {"type": "scene", "value": eid}

    return {"type": "service", "value": service}


def generate_automation_yaml(automation: Dict[str, Any]) -> str:
    name = automation.get("name", "Unnamed")
    trigger = automation.get("trigger") or {}
    action = automation.get("action") or {}

    yaml_data: List[Dict[str, Any]] = [{
        "alias": name,
        "description": "Aangemaakt met Automation Maker",
        "trigger": [],
        "mode": "single",
    }]

    # Trigger
    ttype = trigger.get("type")
    if ttype == "time":
        yaml_data[0]["trigger"].append({"platform": "time", "at": trigger.get("value", "12:00")})

    elif ttype == "state":
        trig = {"platform": "state", "entity_id": trigger.get("value", "")}
        if trigger.get("to") is not None and str(trigger.get("to")).strip() != "":
            trig["to"] = trigger.get("to")
        yaml_data[0]["trigger"].append(trig)

    elif ttype == "sun":
        trig = {"platform": "sun", "event": trigger.get("sunEvent", "sunrise")}
        minutes = str(trigger.get("sunMinutes", "0"))
        try:
            m = int(minutes)
        except Exception:
            m = 0
        if m != 0:
            sign = "-" if trigger.get("sunOffset") == "before" else "+"
            trig["offset"] = f"{sign}00:{m:02d}:00"
        yaml_data[0]["trigger"].append(trig)

    else:
        # safe default
        yaml_data[0]["trigger"].append({"platform": "time", "at": "12:00"})

    # Action
    atype = action.get("type")
    entity_id = (action.get("value") or "").strip()
    domain = entity_id.split(".")[0] if "." in entity_id else ""

    if atype in ("turn_on", "turn_off"):
        if not entity_id:
            action_config = {
                "service": "persistent_notification.create",
                "data": {"title": "Automation Maker", "message": "Geen keuze gemaakt bij DAN"},
            }
        elif domain == "light":
            service = f"light.{atype}"
            action_config: Dict[str, Any] = {"service": service, "target": {"entity_id": entity_id}}

            if atype == "turn_on":
                data_payload: Dict[str, Any] = {}
                rgb = action.get("color_rgb")
                if isinstance(rgb, list) and len(rgb) == 3:
                    data_payload["rgb_color"] = rgb
                brightness = action.get("brightness")
                if brightness is not None and str(brightness).strip() != "":
                    try:
                        data_payload["brightness"] = int(brightness)
                    except Exception:
                        pass
                if data_payload:
                    action_config["data"] = data_payload
        else:
            action_config = {"service": f"homeassistant.{atype}", "target": {"entity_id": entity_id}}

    elif atype == "notify":
        service = action.get("service", "notify.notify")
        action_config = {"service": service, "data": {"message": action.get("value", "")}}

    elif atype == "scene":
        action_config = {"service": "scene.turn_on", "target": {"entity_id": action.get("value", "")}}

    else:
        action_config = {
            "service": "persistent_notification.create",
            "data": {"title": "Automation Maker", "message": "Actie niet herkend"},
        }

    yaml_data[0]["action"] = [action_config]

    return yaml.dump(yaml_data, allow_unicode=True, default_flow_style=False, sort_keys=False)


# -----------------------------------------------------------------------------
# API routes
# -----------------------------------------------------------------------------
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "ok": True,
        "config_path": HA_CONFIG_PATH,
        "automations_path": AUTOMATIONS_PATH,
        "token": bool(SUPERVISOR_TOKEN),
        "ingress_path": ingress_path(),
    })


@app.route("/api/ingress", methods=["GET"])
def api_ingress():
    """Small helper for future UI improvements: returns ingress base path if available."""
    return jsonify({"ingress_path": ingress_path()})


@app.route("/api/entities", methods=["GET"])
def api_entities():
    return jsonify(get_ha_entities())


@app.route("/api/automations", methods=["GET"])
def api_list_automations():
    try:
        if not os.path.exists(AUTOMATIONS_PATH):
            return jsonify([])

        files: List[Dict[str, str]] = []
        for fn in sorted(os.listdir(AUTOMATIONS_PATH)):
            if not fn.endswith(".yaml"):
                continue
            fp = safe_join(AUTOMATIONS_PATH, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    files.append({"filename": fn, "name": content[0].get("alias", "Onbekend")})
                else:
                    files.append({"filename": fn, "name": "Onbekend (ongeldig formaat)"})
            except Exception:
                files.append({"filename": fn, "name": "Onbekend (leesfout)"})

        return jsonify(files)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/automation/<filename>", methods=["GET"])
def api_get_automation(filename: str):
    try:
        fp = safe_join(AUTOMATIONS_PATH, filename)
        if not os.path.exists(fp):
            return jsonify({"error": "Automation niet gevonden"}), 404

        with open(fp, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f)

        if not isinstance(yaml_data, list) or not yaml_data or not isinstance(yaml_data[0], dict):
            return jsonify({"error": "Ongeldig formaat (verwacht lijst met 1 item)"}), 400

        auto_yaml = yaml_data[0]
        automation = {
            "name": auto_yaml.get("alias", "Onbekend"),
            "trigger": parse_trigger_from_yaml(auto_yaml.get("trigger", [])),
            "action": parse_action_from_yaml(auto_yaml.get("action", [])),
        }
        return jsonify({"automation": automation, "filename": os.path.basename(filename)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/automation", methods=["POST"])
def api_create_automation():
    try:
        data = request.json or {}
        if "automation" not in data:
            return jsonify({"error": "Geen automation data ontvangen"}), 400

        automation = data["automation"] or {}
        name = automation.get("name", "unnamed")

        filename = f"{sanitize_filename(name)}.yaml"
        fp = safe_join(AUTOMATIONS_PATH, filename)

        if os.path.exists(fp):
            return jsonify({"error": f'Automation "{name}" bestaat al!'}), 409

        yaml_content = generate_automation_yaml(automation)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        reload_automations()
        return jsonify({"success": True, "message": f'Automation "{name}" opgeslagen!', "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/automation/<filename>", methods=["PUT"])
def api_update_automation(filename: str):
    try:
        data = request.json or {}
        if "automation" not in data:
            return jsonify({"error": "Geen automation data ontvangen"}), 400

        fp = safe_join(AUTOMATIONS_PATH, filename)
        if not os.path.exists(fp):
            return jsonify({"error": "Automation niet gevonden"}), 404

        automation = data["automation"] or {}
        yaml_content = generate_automation_yaml(automation)

        with open(fp, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        reload_automations()
        return jsonify({"success": True, "message": "Automation bijgewerkt!", "filename": os.path.basename(filename)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/automation/<filename>", methods=["DELETE"])
def api_delete_automation(filename: str):
    try:
        fp = safe_join(AUTOMATIONS_PATH, filename)
        if not os.path.exists(fp):
            return jsonify({"error": "Automation niet gevonden"}), 404
        os.remove(fp)
        reload_automations()
        return jsonify({"success": True, "message": "Automation verwijderd"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------------------------
# Test endpoint (human steps + optional tech)
# -----------------------------------------------------------------------------
@app.route("/api/test", methods=["POST"])
def api_test_action():
    try:
        data = request.json or {}
        if "automation" not in data:
            return jsonify({"error": "Geen automation data ontvangen"}), 400

        automation = data["automation"] or {}
        action = automation.get("action") or {}
        atype = action.get("type")
        entity_id = (action.get("value") or "").strip()
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        steps: List[Dict[str, Any]] = []

        def step(msg: str, ok: bool = True, tech: Dict[str, Any] | None = None) -> None:
            # UI expects: {message, ok, extra:{tech:{...}}}
            extra = {"tech": tech} if tech else {}
            steps.append({"message": msg, "ok": ok, "extra": extra})

        step("Test gestart: we doen alsof 'WANNEER' klopt en voeren alleen 'DAN' uit.", True)

        # Turn on/off
        if atype in ("turn_on", "turn_off"):
            if not entity_id:
                return jsonify({"error": "Geen keuze gemaakt bij DAN (kies eerst iets om aan/uit te zetten)."}), 400

            is_on = (atype == "turn_on")

            # Light special
            if domain == "light":
                svc_domain = "light"
                svc_service = "turn_on" if is_on else "turn_off"
                payload: Dict[str, Any] = {"target": {"entity_id": entity_id}}

                if is_on:
                    data_payload: Dict[str, Any] = {}
                    rgb = action.get("color_rgb")
                    if isinstance(rgb, list) and len(rgb) == 3:
                        data_payload["rgb_color"] = rgb
                    brightness = action.get("brightness")
                    if brightness is not None and str(brightness).strip() != "":
                        try:
                            data_payload["brightness"] = int(brightness)
                        except Exception:
                            pass
                    if data_payload:
                        payload["data"] = data_payload

                step(
                    "We geven Home Assistant de opdracht om de lamp aan/uit te zetten.",
                    True,
                    {"call": f"{svc_domain}.{svc_service}", "payload": payload},
                )

                code, text = ha_call_service(svc_domain, svc_service, payload)
                ok = (200 <= code < 300)
                step(
                    "Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                    ok,
                    {"http": code, "response": text},
                )
                return jsonify({"success": ok, "steps": steps})

            # Generic on/off
            svc_domain = "homeassistant"
            svc_service = "turn_on" if is_on else "turn_off"
            payload = {"target": {"entity_id": entity_id}}

            step(
                "We geven Home Assistant de opdracht om iets aan/uit te zetten.",
                True,
                {"call": f"{svc_domain}.{svc_service}", "payload": payload},
            )

            code, text = ha_call_service(svc_domain, svc_service, payload)
            ok = (200 <= code < 300)
            step(
                "Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                ok,
                {"http": code, "response": text},
            )
            return jsonify({"success": ok, "steps": steps})

        # Notify
        if atype == "notify":
            msg = (action.get("value") or "").strip()
            if not msg:
                return jsonify({"error": "Vul eerst een tekst in voor het berichtje."}), 400

            payload = {"data": {"title": "Automation Maker Test", "message": msg}}
            step(
                "We vragen Home Assistant om een berichtje te laten zien.",
                True,
                {"call": "persistent_notification.create", "payload": payload},
            )

            code, text = ha_call_service("persistent_notification", "create", payload)
            ok = (200 <= code < 300)
            step(
                "Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                ok,
                {"http": code, "response": text},
            )
            return jsonify({"success": ok, "steps": steps})

        # Scene
        if atype == "scene":
            if not entity_id:
                return jsonify({"error": "Vul eerst een scene in."}), 400

            payload = {"target": {"entity_id": entity_id}}
            step(
                "We vragen Home Assistant om de sfeer/scene aan te zetten.",
                True,
                {"call": "scene.turn_on", "payload": payload},
            )

            code, text = ha_call_service("scene", "turn_on", payload)
            ok = (200 <= code < 300)
            step(
                "Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                ok,
                {"http": code, "response": text},
            )
            return jsonify({"success": ok, "steps": steps})

        return jsonify({"error": "Actie type niet ondersteund in test"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------------------------
# UI serving (Ingress-friendly)
# -----------------------------------------------------------------------------
@app.errorhandler(404)
def handle_404(_err):
    """
    If someone hits an unknown API URL, return JSON.
    BUT: Home Assistant Ingress URLs may start with /api/hassio_ingress/..., which is NOT our API.
    So we only treat /api/* as API if it's not the ingress prefix.
    """
    path = (request.path or "").lstrip("/")
    if request.path.startswith("/api/") and not request.path.startswith("/api/hassio_ingress/"):
        return jsonify({"error": "Not found"}), 404
    # Otherwise, fall back to index.html (single-page style)
    return send_from_directory("/", "index.html")


@app.route("/", defaults={"path": ""}, methods=["GET"])
@app.route("/<path:path>", methods=["GET"])
def serve_ui(path: str):
    """
    Catch-all UI route:
    - Works for normal access
    - Works for Ingress paths (even if the ingress proxy doesn't strip prefixes)
    - API routes are defined above; they will match before this route
    """
    # If it's exactly our index request or any "unknown" path, serve index.html
    return send_from_directory("/", "index.html")


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Automation Maker Starting…")
    print("=" * 60)
    print(f"Config path: {HA_CONFIG_PATH}")
    print(f"Automations path: {AUTOMATIONS_PATH}")
    print(f"Supervisor token: {'Available' if SUPERVISOR_TOKEN else 'Missing'}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=8099, debug=False)
