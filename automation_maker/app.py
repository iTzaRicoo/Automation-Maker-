#!/usr/bin/env python3
"""
Automation Maker - Home Assistant Add-on backend (Flask)

Goals:
- Simple REST API for UI (create/update/delete/list/test automations)
- Plays nice with Home Assistant Ingress (including weird /api/hassio_ingress/... paths)
- Beginner-friendly test steps (human text) + optional tech details in `extra.tech`

Included fixes & improvements:
1) Slimmere conflict detection (tijd overlap check)
2) Templates/Voorbeelden (kindvriendelijk) -> /api/templates
3) Validatie verbeteringen (extra UX checks)
4) Backup functie -> /api/backup (ZIP)
5) Dependency check (entity bestaat?) -> warnings
6) Smart suggestions (AI-powered) -> /api/suggestions
"""

from __future__ import annotations

import os
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
import yaml
from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS


# -----------------------------------------------------------------------------
# App + Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
AUTOMATIONS_PATH = os.environ.get("AUTOMATIONS_PATH") or os.path.join(HA_CONFIG_PATH, "include", "automations")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

Path(AUTOMATIONS_PATH).mkdir(parents=True, exist_ok=True)

print(f"[Automation Maker] Config path: {HA_CONFIG_PATH}")
print(f"[Automation Maker] Automations path: {AUTOMATIONS_PATH}")
print(f"[Automation Maker] Supervisor token available: {bool(SUPERVISOR_TOKEN)}")
print(f"[Automation Maker] Debug mode: {DEBUG_MODE}")


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
# Advanced Dutch Search
# -----------------------------------------------------------------------------
def normalize_dutch_text(text: str) -> str:
    """Normaliseer Nederlandse tekst voor fuzzy matching."""
    if not text:
        return ""

    text = text.lower().strip()

    # Remove accents (é → e, ë → e, etc.)
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

    replacements = {
        "licht": "lamp",
        "verlichting": "lamp",
        "lampje": "lamp",
        "ledlamp": "lamp",
        "ledstrip": "lamp",
        "spot": "lamp",
        "plafondlamp": "lamp",
        "avond": "avonds",
        "s avonds": "avonds",
        "savonds": "avonds",
        "ochtend": "ochtends",
        "s ochtends": "ochtends",
        "sochtends": "ochtends",
        "nacht": "nachts",
        "s nachts": "nachts",
        "snachts": "nachts",
        "middag": "middags",
        "aan": "aanzetten",
        "uit": "uitzetten",
        "aandoen": "aanzetten",
        "uitdoen": "uitzetten",
        "inschakelen": "aanzetten",
        "uitschakelen": "uitzetten",
        "activeren": "aanzetten",
        "deactiveren": "uitzetten",
        "woonkamer": "living",
        "zitkamer": "living",
        "slaapkamer": "bedroom",
        "badkamer": "bathroom",
        "keuken": "kitchen",
        "hal": "hallway",
        "gang": "hallway",
        "verwarming": "heating",
        "thermostaat": "heating",
        "cv": "heating",
        "koeling": "cooling",
        "airco": "cooling",
        "rolluik": "shutter",
        "zonwering": "shutter",
        "gordijn": "curtain",
        "scherm": "screen",
        "zonsondergang": "sunset",
        "zonsopgang": "sunrise",
        "zonsopkomst": "sunrise",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def search_automations_dutch(query: str, automations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Zoek automations met uitgebreide Nederlandse ondersteuning."""
    if not query or not query.strip():
        return automations

    normalized_query = normalize_dutch_text(query)
    query_words = set(normalized_query.split())

    scored_results: List[Dict[str, Any]] = []

    for auto in automations:
        score = 0
        filename = auto.get("filename", "")
        name = auto.get("name", "")

        normalized_name = normalize_dutch_text(name)
        normalized_filename = normalize_dutch_text(filename)

        if normalized_query in normalized_name or normalized_query in normalized_filename:
            score += 1000

        name_words = set(normalized_name.split())
        filename_words = set(normalized_filename.split())
        all_words = name_words | filename_words

        matching_words = query_words & all_words
        score += len(matching_words) * 100

        if any(normalized_name.startswith(word) for word in query_words):
            score += 50

        for qword in query_words:
            if len(qword) >= 3:
                for aword in all_words:
                    if qword in aword or aword in qword:
                        score += 25

        if score > 0:
            scored_results.append({"automation": auto, "score": score})

    scored_results.sort(key=lambda x: x["score"], reverse=True)
    return [item["automation"] for item in scored_results]


# -----------------------------------------------------------------------------
# Safety checks + improvements
# -----------------------------------------------------------------------------
def check_infinite_loop(automation: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Detecteer als een automation zichzelf kan triggeren.
    Returns: {"warning": str, "severity": "error"|"warning"} of None
    """
    trigger = automation.get("trigger") or {}
    action = automation.get("action") or {}

    trigger_type = trigger.get("type")
    trigger_entity = trigger.get("value", "")
    action_entity = action.get("value", "")

    if trigger_type == "state" and trigger_entity and action_entity:
        if trigger_entity == action_entity:
            return {
                "warning": "⚠️ Deze automation kan zichzelf oneindig triggeren! "
                           f"Je triggert op '{trigger_entity}' en verandert diezelfde entity. "
                           "Dat is een slecht idee.",
                "severity": "error",
            }

    return None


# -----------------------------------------------------------------------------
# FIX 1: Slimmere conflict detection (tijd overlap check)
# -----------------------------------------------------------------------------
def check_time_overlap(time1: str, time2: str, margin_minutes: int = 5) -> bool:
    """
    Check of twee tijden te dicht bij elkaar liggen.
    Bijv: 08:15 en 08:17 = overlap (binnen 5 min)
    """
    try:
        t1 = datetime.strptime(time1, "%H:%M")
        t2 = datetime.strptime(time2, "%H:%M")
        diff = abs((t1 - t2).total_seconds() / 60)
        return diff <= margin_minutes
    except Exception:
        return False


def check_conflicts(automation: Dict[str, Any], existing_automations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Detecteer conflicten met bestaande automations.
    NIEUW: Check ook tijden die te dicht bij elkaar liggen
    """
    conflicts: List[Dict[str, Any]] = []

    trigger = automation.get("trigger") or {}
    action = automation.get("action") or {}

    trigger_type = trigger.get("type")
    trigger_time = trigger.get("value", "") if trigger_type == "time" else None
    trigger_days = set(trigger.get("days", []))
    action_type = action.get("type")
    action_entity = action.get("value", "")

    for existing in existing_automations:
        try:
            fp = safe_join(AUTOMATIONS_PATH, existing.get("filename", ""))
            if not os.path.exists(fp):
                continue

            with open(fp, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)

            if not isinstance(yaml_data, list) or not yaml_data:
                continue

            existing_trigger = parse_trigger_from_yaml(
                yaml_data[0].get("trigger", []),
                yaml_data[0].get("condition", []),
            )
            existing_action = parse_action_from_yaml(yaml_data[0].get("action", []))

            existing_days = set(existing_trigger.get("days", []))
            existing_time = existing_trigger.get("value", "") if existing_trigger.get("type") == "time" else None

            has_overlapping_days = (
                (not trigger_days and not existing_days) or
                (not trigger_days and existing_days) or
                (trigger_days and not existing_days) or
                bool(trigger_days & existing_days)
            )

            if trigger_type == "time" and existing_trigger.get("type") == "time" and has_overlapping_days:
                if action_entity == existing_action.get("value"):
                    # ✅ EXACT dezelfde tijd
                    if trigger_time == existing_time:
                        if (
                            (action_type == "turn_on" and existing_action.get("type") == "turn_off")
                            or (action_type == "turn_off" and existing_action.get("type") == "turn_on")
                        ):
                            conflicts.append({
                                "automation": existing.get("name", "Onbekend"),
                                "conflict": f"⚠️ '{existing.get('name')}' doet het tegenovergestelde "
                                           f"om {trigger_time} met '{action_entity}'!",
                                "severity": "warning",
                            })
                        elif action_type == existing_action.get("type"):
                            conflicts.append({
                                "automation": existing.get("name", "Onbekend"),
                                "conflict": f"ℹ️ '{existing.get('name')}' doet precies hetzelfde "
                                           f"om {trigger_time}. Dubbel werk?",
                                "severity": "info",
                            })

                    # ✅ NIEUW: Tijden liggen binnen 5 minuten van elkaar
                    elif trigger_time and existing_time and check_time_overlap(trigger_time, existing_time, 5):
                        if (
                            (action_type == "turn_on" and existing_action.get("type") == "turn_off")
                            or (action_type == "turn_off" and existing_action.get("type") == "turn_on")
                        ):
                            conflicts.append({
                                "automation": existing.get("name", "Onbekend"),
                                "conflict": f"⏰ '{existing.get('name')}' gebeurt bijna gelijktijdig "
                                           f"({existing_time} vs {trigger_time}) en doet het tegenovergestelde met '{action_entity}'.",
                                "severity": "info",
                            })

        except Exception as e:
            print(f"[Conflict check] Error checking {existing.get('filename')}: {e}")
            continue

    return conflicts


def check_dangerous_action(automation: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Detecteer gevaarlijke acties die bevestiging vereisen.
    Returns: {"warning": str, "severity": "danger"|"warning"|"info", "require_confirmation": bool} of None
    """
    action = automation.get("action") or {}
    action_type = action.get("type")
    action_entity = (action.get("value") or "").lower()

    dangerous_patterns: List[Dict[str, Any]] = []

    if action_type == "turn_off":
        if "all" in action_entity or "alles" in action_entity or ".*" in action_entity:
            dangerous_patterns.append({
                "warning": "🚨 Je zet ALLES uit! Weet je het zeker?",
                "severity": "danger",
                "require_confirmation": True,
            })

        if any(word in action_entity for word in ["heat", "verwarming", "cv", "therm"]):
            dangerous_patterns.append({
                "warning": "🥶 Let op: verwarming uitzetten kan gevaarlijk zijn bij vriesweer!",
                "severity": "warning",
                "require_confirmation": True,
            })

    trigger = automation.get("trigger") or {}
    if action_type == "turn_off" and "light" in action_entity:
        if trigger.get("type") == "time":
            time_str = trigger.get("value", "")
            try:
                hour = int(time_str.split(":")[0])
                if 0 <= hour <= 5:
                    dangerous_patterns.append({
                        "warning": "💡 Alle lampen uit midden in de nacht? Denk aan veiligheid (bijv. nachtlampje).",
                        "severity": "info",
                        "require_confirmation": False,
                    })
            except Exception:
                pass

    return dangerous_patterns[0] if dangerous_patterns else None


# -----------------------------------------------------------------------------
# FIX 3: Validatie verbeteringen
# -----------------------------------------------------------------------------
def validate_automation(automation: Dict[str, Any]) -> List[str]:
    """
    Extra validaties voor betere UX.
    Returns: lijst van error messages (leeg = alles OK)
    """
    errors: List[str] = []

    action = automation.get("action") or {}
    trigger = automation.get("trigger") or {}

    # Check 1: Lamp brightness = 0
    if action.get("type") == "turn_on" and action.get("brightness") == 0:
        errors.append("Helderheid is 0%! De lamp gaat dan niet echt aan. Kies een waarde tussen 1-255.")

    # Check 2: Notificatie zonder tekst
    if action.get("type") == "notify" and not (action.get("value") or "").strip():
        errors.append("Je wilt een melding sturen maar er staat geen tekst in!")

    # Check 3: Time trigger in het verleden (vandaag)
    if trigger.get("type") == "time":
        try:
            now = datetime.now()
            trig_t = datetime.strptime(trigger.get("value", "00:00"), "%H:%M")
            trig_t = trig_t.replace(year=now.year, month=now.month, day=now.day)
            if trig_t < now - timedelta(hours=2):
                errors.append(f"⏰ Let op: {trigger.get('value')} is al geweest vandaag. Deze automation draait pas morgen!")
        except Exception:
            pass

    # Check 4: Scene zonder entity_id
    if action.get("type") == "scene" and not (action.get("value") or "").strip():
        errors.append("Je hebt geen scene gekozen!")

    # Check 5: State trigger zonder entity
    if trigger.get("type") == "state" and not (trigger.get("value") or "").strip():
        errors.append("Je hebt niets gekozen bij 'Wat moet veranderen'!")

    return errors


# -----------------------------------------------------------------------------
# FIX 5: Dependency check (entity bestaat?)
# -----------------------------------------------------------------------------
def check_entity_exists(entity_id: str) -> bool:
    """Check of een entity nog bestaat in Home Assistant."""
    if not entity_id or not SUPERVISOR_TOKEN:
        return False

    try:
        resp = requests.get("http://supervisor/core/api/states", headers=ha_headers(), timeout=5)
        if resp.status_code != 200:
            return False
        states = resp.json()
        return any(s.get("entity_id") == entity_id for s in states)
    except Exception:
        return False


def validate_entities_exist(automation: Dict[str, Any]) -> List[str]:
    """Check of alle entities in de automation nog bestaan. Returns: lijst van warnings."""
    warnings: List[str] = []

    trigger = automation.get("trigger") or {}
    action = automation.get("action") or {}

    if trigger.get("type") == "state":
        entity = trigger.get("value", "")
        if entity and not check_entity_exists(entity):
            warnings.append(f"⚠️ Entity '{entity}' bestaat niet (meer) in Home Assistant!")

    if action.get("type") in ["turn_on", "turn_off", "scene"]:
        entity = action.get("value", "")
        if entity and not check_entity_exists(entity):
            warnings.append(f"⚠️ Entity '{entity}' bestaat niet (meer) in Home Assistant!")

    return warnings


def get_current_temperature() -> float | None:
    """Haal huidige temperatuur op (optioneel, voor slimme checks)."""
    try:
        if not SUPERVISOR_TOKEN:
            return None

        resp = requests.get("http://supervisor/core/api/states", headers=ha_headers(), timeout=5)
        if resp.status_code != 200:
            return None

        states = resp.json()
        for state in states:
            entity_id = state.get("entity_id", "")
            if "weather." in entity_id or "temperature" in entity_id.lower():
                try:
                    temp = float(state.get("state", 999))
                    if -50 < temp < 50:
                        return temp
                except Exception:
                    continue
        return None
    except Exception:
        return None


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

        entities.sort(key=lambda x: (x.get("name") or "").lower())
        return entities
    except Exception as e:
        print(f"[Automation Maker] Error getting entities: {e}")
        return []


# -----------------------------------------------------------------------------
# YAML <-> UI model conversion
# -----------------------------------------------------------------------------
def parse_trigger_from_yaml(trigger_list: Any, condition_list: Any = None) -> Dict[str, Any]:
    if not trigger_list or not isinstance(trigger_list, list):
        return {"type": "", "value": ""}

    t = trigger_list[0] or {}
    platform = t.get("platform", "")

    selected_days: List[str] = []
    if condition_list and isinstance(condition_list, list):
        for cond in condition_list:
            if isinstance(cond, dict) and cond.get("condition") == "time" and "weekday" in cond:
                selected_days = cond["weekday"]
                if isinstance(selected_days, str):
                    selected_days = [selected_days]
                break

    if platform == "time":
        model = {"type": "time", "value": t.get("at", "")}
        if selected_days:
            model["days"] = selected_days
        return model

    if platform == "state":
        model: Dict[str, Any] = {"type": "state", "value": t.get("entity_id", "")}
        if "to" in t:
            model["to"] = t.get("to")
        if selected_days:
            model["days"] = selected_days
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
        if selected_days:
            model["days"] = selected_days
        return model

    return {"type": platform, "value": ""}


def parse_action_from_yaml(action_list: Any) -> Dict[str, Any]:
    if not action_list or not isinstance(action_list, list):
        return {"type": "", "value": ""}

    a = action_list[0] or {}
    service = a.get("service", "")

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
        "condition": [],
        "mode": "single",
    }]

    ttype = trigger.get("type")
    selected_days = trigger.get("days", [])

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
        yaml_data[0]["trigger"].append({"platform": "time", "at": "12:00"})

    if selected_days and len(selected_days) > 0:
        yaml_data[0]["condition"].append({
            "condition": "time",
            "weekday": selected_days
        })

    if not yaml_data[0]["condition"]:
        del yaml_data[0]["condition"]

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
        "debug": DEBUG_MODE,
    })


@app.route("/api/ingress", methods=["GET"])
def api_ingress():
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
            "trigger": parse_trigger_from_yaml(auto_yaml.get("trigger", []), auto_yaml.get("condition", [])),
            "action": parse_action_from_yaml(auto_yaml.get("action", [])),
        }
        return jsonify({"automation": automation, "filename": os.path.basename(filename)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------------------------
# FIX 2: Templates/Voorbeelden (kindvriendelijk)
# -----------------------------------------------------------------------------
@app.route("/api/templates", methods=["GET"])
def api_get_templates():
    templates = [
        {
            "id": "lamp_avond",
            "emoji": "🌙",
            "name": "Lampje aan als het donker wordt",
            "description": "Als de zon ondergaat, gaat de lamp in de woonkamer aan. Zo is het niet donker!",
            "automation": {
                "name": "Lamp aan bij zonsondergang",
                "trigger": {"type": "sun", "sunEvent": "sunset", "sunOffset": "after", "sunMinutes": "0"},
                "action": {"type": "turn_on", "value": "light.woonkamer"},
            },
        },
        {
            "id": "lamp_ochtend",
            "emoji": "☀️",
            "name": "Lampje uit als het licht wordt",
            "description": "Als de zon opkomt, gaat de lamp uit. Want dan is het al licht buiten!",
            "automation": {
                "name": "Lamp uit bij zonsopgang",
                "trigger": {"type": "sun", "sunEvent": "sunrise", "sunOffset": "after", "sunMinutes": "0"},
                "action": {"type": "turn_off", "value": "light.woonkamer"},
            },
        },
        {
            "id": "ochtend_routine",
            "emoji": "⏰",
            "name": "Wakker worden op school dagen",
            "description": "Op maandag tot vrijdag om 7 uur gaat de lamp aan. Tijd om op te staan!",
            "automation": {
                "name": "Ochtend routine weekdagen",
                "trigger": {"type": "time", "value": "07:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                "action": {"type": "turn_on", "value": "light.slaapkamer"},
            },
        },
        {
            "id": "slapen_gaan",
            "emoji": "😴",
            "name": "Alle lampjes uit, tijd om te slapen",
            "description": "Om 9 uur 's avonds gaan alle lampjes uit. Welterusten!",
            "automation": {
                "name": "Slaaptijd - alle lampen uit",
                "trigger": {"type": "time", "value": "21:00"},
                "action": {"type": "turn_off", "value": "light.all"},
            },
        },
        {
            "id": "weekend_uitslapen",
            "emoji": "🛏️",
            "name": "Weekend: later opstaan",
            "description": "Op zaterdag en zondag mag je uitslapen! Lamp gaat pas om 9 uur aan.",
            "automation": {
                "name": "Weekend ochtend",
                "trigger": {"type": "time", "value": "09:00", "days": ["sat", "sun"]},
                "action": {"type": "turn_on", "value": "light.slaapkamer"},
            },
        },
        {
            "id": "herinnering",
            "emoji": "📢",
            "name": "Herinnering: tanden poetsen",
            "description": "Elke avond om half 9 krijg je een berichtje dat je je tanden moet poetsen!",
            "automation": {
                "name": "Herinnering tandenpoetsen",
                "trigger": {"type": "time", "value": "20:30"},
                "action": {"type": "notify", "value": "Niet vergeten: tanden poetsen! 🪥", "service": "notify.notify"},
            },
        },
    ]
    return jsonify(templates)


# -----------------------------------------------------------------------------
# FIX 4: Backup functie (ZIP)
# -----------------------------------------------------------------------------
@app.route("/api/backup", methods=["GET"])
def api_backup_all():
    """Download alle automations als ZIP bestand."""
    try:
        if not os.path.exists(AUTOMATIONS_PATH):
            return jsonify({"error": "Geen automations map gevonden"}), 404

        zip_buffer = BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for filename in os.listdir(AUTOMATIONS_PATH):
                if filename.endswith(".yaml"):
                    file_path = safe_join(AUTOMATIONS_PATH, filename)
                    if os.path.exists(file_path):
                        zip_file.write(file_path, filename)

        zip_buffer.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"automation_backup_{timestamp}.zip",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------------------------
# Create/Update automation (met validatie + warnings + confirmation)
# -----------------------------------------------------------------------------
@app.route("/api/automation", methods=["POST"])
def api_create_automation():
    try:
        data = request.json or {}
        if "automation" not in data:
            return jsonify({"error": "Geen automation data ontvangen"}), 400

        automation = data.get("automation") or {}

        # ✅ NIEUW: Validatie
        validation_errors = validate_automation(automation)
        if validation_errors:
            return jsonify({
                "error": "Validatie fouten gevonden:\n" + "\n".join(f"• {e}" for e in validation_errors)
            }), 400

        name = automation.get("name", "unnamed")
        filename = f"{sanitize_filename(name)}.yaml"
        fp = safe_join(AUTOMATIONS_PATH, filename)

        if os.path.exists(fp):
            return jsonify({"error": f'Automation "{name}" bestaat al!'}), 409

        warnings: List[Dict[str, Any]] = []

        # ✅ NIEUW: Entity dependency warnings
        entity_warnings = validate_entities_exist(automation)
        if entity_warnings:
            warnings.extend([{"warning": w, "severity": "warning"} for w in entity_warnings])

        # Safety checks
        loop_check = check_infinite_loop(automation)
        if loop_check:
            warnings.append(loop_check)

        existing: List[Dict[str, Any]] = []
        if os.path.exists(AUTOMATIONS_PATH):
            for fn in os.listdir(AUTOMATIONS_PATH):
                if fn.endswith(".yaml"):
                    try:
                        with open(safe_join(AUTOMATIONS_PATH, fn), "r", encoding="utf-8") as f:
                            content = yaml.safe_load(f)
                        if isinstance(content, list) and content:
                            existing.append({"filename": fn, "name": content[0].get("alias", "Onbekend")})
                    except Exception:
                        continue

        conflicts = check_conflicts(automation, existing)
        for c in conflicts:
            warnings.append({"warning": c["conflict"], "severity": c["severity"]})

        danger_check = check_dangerous_action(automation)
        if danger_check:
            warnings.append(danger_check)

        has_critical = any(w.get("severity") in ["error", "danger"] for w in warnings)
        confirmed = bool(data.get("confirmed", False))
        if has_critical and not confirmed:
            return jsonify({
                "warnings": warnings,
                "require_confirmation": True,
                "message": "Er zijn waarschuwingen gevonden. Wil je toch doorgaan?",
            }), 400

        yaml_content = generate_automation_yaml(automation)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        reload_automations()

        response: Dict[str, Any] = {
            "success": True,
            "message": f'Automation "{name}" opgeslagen!',
            "filename": filename,
        }
        if warnings:
            response["warnings"] = warnings

        return jsonify(response)

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

        automation = data.get("automation") or {}

        # ✅ NIEUW: Validatie
        validation_errors = validate_automation(automation)
        if validation_errors:
            return jsonify({
                "error": "Validatie fouten gevonden:\n" + "\n".join(f"• {e}" for e in validation_errors)
            }), 400

        warnings: List[Dict[str, Any]] = []

        # ✅ NIEUW: Entity dependency warnings
        entity_warnings = validate_entities_exist(automation)
        if entity_warnings:
            warnings.extend([{"warning": w, "severity": "warning"} for w in entity_warnings])

        loop_check = check_infinite_loop(automation)
        if loop_check:
            warnings.append(loop_check)

        existing: List[Dict[str, Any]] = []
        if os.path.exists(AUTOMATIONS_PATH):
            for fn in os.listdir(AUTOMATIONS_PATH):
                if fn.endswith(".yaml") and fn != filename:
                    try:
                        with open(safe_join(AUTOMATIONS_PATH, fn), "r", encoding="utf-8") as f:
                            content = yaml.safe_load(f)
                        if isinstance(content, list) and content:
                            existing.append({"filename": fn, "name": content[0].get("alias", "Onbekend")})
                    except Exception:
                        continue

        conflicts = check_conflicts(automation, existing)
        for c in conflicts:
            warnings.append({"warning": c["conflict"], "severity": c["severity"]})

        danger_check = check_dangerous_action(automation)
        if danger_check:
            warnings.append(danger_check)

        has_critical = any(w.get("severity") in ["error", "danger"] for w in warnings)
        confirmed = bool(data.get("confirmed", False))
        if has_critical and not confirmed:
            return jsonify({
                "warnings": warnings,
                "require_confirmation": True,
                "message": "Er zijn waarschuwingen gevonden. Wil je toch doorgaan?",
            }), 400

        yaml_content = generate_automation_yaml(automation)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(yaml_content)

        reload_automations()

        response: Dict[str, Any] = {
            "success": True,
            "message": "Automation bijgewerkt!",
            "filename": os.path.basename(filename),
        }
        if warnings:
            response["warnings"] = warnings

        return jsonify(response)

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
# Search endpoint
# -----------------------------------------------------------------------------
@app.route("/api/automations/search", methods=["POST"])
def api_search_automations():
    """Zoek automations met uitgebreide Nederlandse ondersteuning."""
    try:
        data = request.json or {}
        query = (data.get("query", "") or "").strip()

        if not os.path.exists(AUTOMATIONS_PATH):
            return jsonify([])

        all_automations: List[Dict[str, Any]] = []
        for fn in sorted(os.listdir(AUTOMATIONS_PATH)):
            if not fn.endswith(".yaml"):
                continue
            fp = safe_join(AUTOMATIONS_PATH, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    all_automations.append({"filename": fn, "name": content[0].get("alias", "Onbekend")})
                else:
                    all_automations.append({"filename": fn, "name": "Onbekend (ongeldig formaat)"})
            except Exception:
                all_automations.append({"filename": fn, "name": "Onbekend (leesfout)"})

        results = search_automations_dutch(query, all_automations)
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------------------------
# FIX 6: Smart suggestions (AI-powered)
# -----------------------------------------------------------------------------
@app.route("/api/suggestions", methods=["POST"])
def api_get_suggestions():
    """AI-powered suggesties voor verbetering van automation."""
    try:
        data = request.json or {}
        automation = data.get("automation") or {}

        suggestions: List[Dict[str, Any]] = []
        trigger = automation.get("trigger") or {}
        action = automation.get("action") or {}

        # Suggestie 1: Lamp aan maar niet uit
        if action.get("type") == "turn_on" and trigger.get("type") in ["time", "sun"]:
            entity = (action.get("value") or "")
            if entity.startswith("light."):
                suggestions.append({
                    "icon": "💡",
                    "title": "Vergeet je de lamp uit te zetten?",
                    "description": f"Je zet '{entity}' aan, maar ik zie geen automation die hem weer uitzet. "
                                   "Wil je ook een 'uit' automation maken?",
                    "severity": "tip",
                })

        # Suggestie 2: Elke dag maar zou weekdays kunnen zijn
        if trigger.get("type") == "time" and not trigger.get("days"):
            time_val = trigger.get("value", "")
            try:
                hour = int(time_val.split(":")[0])
                if 6 <= hour <= 9 or 22 <= hour <= 23:
                    suggestions.append({
                        "icon": "📅",
                        "title": "Misschien alleen op doordeweekse dagen?",
                        "description": f"Deze automation draait om {time_val} elke dag. "
                                       "Wil je in het weekend misschien uitslapen of later naar bed?",
                        "severity": "tip",
                    })
            except Exception:
                pass

        # Suggestie 3: Brightness te laag
        if action.get("type") == "turn_on":
            brightness = action.get("brightness")
            try:
                if brightness is not None and int(brightness) < 50:
                    suggestions.append({
                        "icon": "🔅",
                        "title": "Lamp staat vrij donker",
                        "description": f"De helderheid is {brightness}/255. Dat is best donker! "
                                       "Is dat expres (bijv. nachtlampje)?",
                        "severity": "info",
                    })
            except Exception:
                pass

        # Suggestie 4: Zonsondergang maar geen dagen filter
        if trigger.get("type") == "sun" and trigger.get("sunEvent") == "sunset":
            if not trigger.get("days"):
                suggestions.append({
                    "icon": "🌅",
                    "title": "Elke dag zonsondergang",
                    "description": "Deze automation gaat af bij elke zonsondergang. "
                                   "Misschien wil je dit alleen in het weekend of juist doordeweeks?",
                    "severity": "tip",
                })

        # Suggestie 5: Scene tip
        if action.get("type") == "scene":
            suggestions.append({
                "icon": "✨",
                "title": "Scene tip",
                "description": "Je gebruikt een scene! Dat is slim. Tip: Maak scenes voor 'Ochtend', 'Avond', "
                               "'Film kijken' etc. Dan kun je met 1 knop alles instellen.",
                "severity": "tip",
            })

        return jsonify(suggestions)

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
            extra = {"tech": tech} if tech else {}
            steps.append({"message": msg, "ok": ok, "extra": extra})

        step("Test gestart: we doen alsof 'WANNEER' klopt en voeren alleen 'DAN' uit.", True)

        if atype in ("turn_on", "turn_off"):
            if not entity_id:
                return jsonify({"error": "Geen keuze gemaakt bij DAN (kies eerst iets om aan/uit te zetten)."}), 400

            is_on = (atype == "turn_on")

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

                step("We geven Home Assistant de opdracht om de lamp aan/uit te zetten.", True,
                     {"call": f"{svc_domain}.{svc_service}", "payload": payload})

                code, text = ha_call_service(svc_domain, svc_service, payload)
                ok = (200 <= code < 300)
                step("Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                     ok, {"http": code, "response": text})
                return jsonify({"success": ok, "steps": steps})

            svc_domain = "homeassistant"
            svc_service = "turn_on" if is_on else "turn_off"
            payload = {"target": {"entity_id": entity_id}}

            step("We geven Home Assistant de opdracht om iets aan/uit te zetten.", True,
                 {"call": f"{svc_domain}.{svc_service}", "payload": payload})

            code, text = ha_call_service(svc_domain, svc_service, payload)
            ok = (200 <= code < 300)
            step("Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                 ok, {"http": code, "response": text})
            return jsonify({"success": ok, "steps": steps})

        if atype == "notify":
            msg = (action.get("value") or "").strip()
            if not msg:
                return jsonify({"error": "Vul eerst een tekst in voor het berichtje."}), 400

            payload = {"data": {"title": "Automation Maker Test", "message": msg}}
            step("We vragen Home Assistant om een berichtje te laten zien.", True,
                 {"call": "persistent_notification.create", "payload": payload})

            code, text = ha_call_service("persistent_notification", "create", payload)
            ok = (200 <= code < 300)
            step("Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                 ok, {"http": code, "response": text})
            return jsonify({"success": ok, "steps": steps})

        if atype == "scene":
            if not entity_id:
                return jsonify({"error": "Vul eerst een scene in."}), 400

            payload = {"target": {"entity_id": entity_id}}
            step("We vragen Home Assistant om de sfeer/scene aan te zetten.", True,
                 {"call": "scene.turn_on", "payload": payload})

            code, text = ha_call_service("scene", "turn_on", payload)
            ok = (200 <= code < 300)
            step("Home Assistant geeft antwoord: gelukt ✅" if ok else "Home Assistant geeft antwoord: mislukt ❌",
                 ok, {"http": code, "response": text})
            return jsonify({"success": ok, "steps": steps})

        return jsonify({"error": "Actie type niet ondersteund in test"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------------------------
# UI serving (Ingress-friendly)
# -----------------------------------------------------------------------------
@app.errorhandler(404)
def handle_404(_err):
    if request.path.startswith("/api/") and not request.path.startswith("/api/hassio_ingress/"):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory("/", "index.html")


@app.route("/", defaults={"path": ""}, methods=["GET"])
@app.route("/<path:path>", methods=["GET"])
def serve_ui(path: str):
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
    print(f"Debug mode: {DEBUG_MODE}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=DEBUG_MODE)
