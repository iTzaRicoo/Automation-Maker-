#!/usr/bin/env python3
from __future__ import annotations

from flask import Flask, request, jsonify, Response
import os
import re
from pathlib import Path
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import yaml

APP_VERSION = "2.1.1-dashboard-maker+fixed-html"
APP_NAME = "Mushroom Dashboard Maker"

app = Flask(__name__)

HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
DASHBOARDS_PATH = os.environ.get("DASHBOARDS_PATH") or os.path.join(HA_CONFIG_PATH, "dashboards")

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
    tok = (os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()
    if tok:
        return tok
    tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()
    if tok:
        return tok
    for p in ("/var/run/supervisor_token", "/run/supervisor_token"):
        tok = _read_file(p)
        if tok:
            return tok
    return ""

SUPERVISOR_TOKEN = discover_token()

Path(DASHBOARDS_PATH).mkdir(parents=True, exist_ok=True)

print(f"== {APP_NAME} {APP_VERSION} ==")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Dashboards path: {DASHBOARDS_PATH}")
print(f"Token available: {bool(SUPERVISOR_TOKEN)}")

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
        if isinstance(data, str) and "\n" in data:
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
    out: List[str] = []
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

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

# -------------------------
# Home Assistant API (Supervisor proxy)
# -------------------------
def ha_request(method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
    url = f"http://supervisor/core{path}"
    return requests.request(method, url, headers=ha_headers(), json=json_body, timeout=timeout)

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

def get_states() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        return [
            {"entity_id": "light.woonkamer", "state": "off", "attributes": {"friendly_name": "Woonkamer Lamp"}},
            {"entity_id": "sensor.temp_woonkamer", "state": "21.1", "attributes": {"friendly_name": "Temp Woonkamer", "unit_of_measurement": "°C", "device_class": "temperature"}},
            {"entity_id": "sensor.woonkamer_rssi", "state": "-62", "attributes": {"friendly_name": "Woonkamer RSSI", "unit_of_measurement": "dBm", "device_class": "signal_strength"}},
            {"entity_id": "binary_sensor.deur_voordeur", "state": "off", "attributes": {"friendly_name": "Voordeur"}},
            {"entity_id": "scene.nacht", "state": "scening", "attributes": {"friendly_name": "Nacht"}},
        ]
    try:
        resp = ha_request("GET", "/api/states", timeout=12)
        if resp.status_code != 200:
            print(f"Failed to fetch states: {resp.status_code} - {resp.text[:200]}")
            return []
        return resp.json()
    except Exception as e:
        print(f"Error getting states: {e}")
        return []

def get_area_registry() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        return [{"area_id": "woonkamer", "name": "Woonkamer (Beneden)"}, {"area_id": "slaapkamer", "name": "Slaapkamer (Boven)"}]
    try:
        resp = ha_request("GET", "/api/config/area_registry", timeout=12)
        if resp.status_code != 200:
            print(f"Failed area_registry: {resp.status_code} - {resp.text[:200]}")
            return []
        return resp.json()
    except Exception as e:
        print(f"Error area_registry: {e}")
        return []

def get_entity_registry() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        return [
            {"entity_id": "light.woonkamer", "area_id": "woonkamer"},
            {"entity_id": "sensor.temp_woonkamer", "area_id": "woonkamer"},
            {"entity_id": "sensor.woonkamer_rssi", "area_id": "woonkamer"},
            {"entity_id": "binary_sensor.deur_voordeur", "area_id": None},
            {"entity_id": "scene.nacht", "area_id": None},
        ]
    try:
        resp = ha_request("GET", "/api/config/entity_registry", timeout=12)
        if resp.status_code != 200:
            print(f"Failed entity_registry: {resp.status_code} - {resp.text[:200]}")
            return []
        return resp.json()
    except Exception as e:
        print(f"Error entity_registry: {e}")
        return []

def build_entities_enriched() -> List[Dict[str, Any]]:
    states = get_states()
    ent_reg = get_entity_registry()

    area_by_entity: Dict[str, Optional[str]] = {}
    for r in ent_reg:
        eid = r.get("entity_id")
        if eid:
            area_by_entity[eid] = r.get("area_id")

    out: List[Dict[str, Any]] = []
    for s in states:
        entity_id = s.get("entity_id", "")
        if not entity_id:
            continue
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        attrs = s.get("attributes") or {}
        friendly = attrs.get("friendly_name") or entity_id
        out.append({
            "entity_id": entity_id,
            "domain": domain,
            "name": friendly,
            "area_id": area_by_entity.get(entity_id),
            "device_class": attrs.get("device_class"),
            "unit": attrs.get("unit_of_measurement"),
            "state_class": attrs.get("state_class"),
        })
    return out

# -------------------------
# Smart filters (anti-clutter)
# -------------------------
DEFAULT_IGNORE_ENTITY_ID_SUFFIXES = [
    "_rssi", "_linkquality", "_lqi", "_signal_strength", "_signal", "_snr",
    "_last_seen", "_lastseen", "_lastupdate",
    "_uptime", "_available", "_availability",
]
DEFAULT_IGNORE_ENTITY_ID_CONTAINS = [
    "linkquality", "rssi", "lqi", "snr", "signal", "last_seen", "lastseen", "uptime",
    "diagnostic", "debug", "heap", "stack", "watchdog",
]
DEFAULT_IGNORE_DEVICE_CLASSES = {"signal_strength"}
DEFAULT_ALLOWED_DOMAINS = {
    "light", "switch", "climate", "media_player", "cover", "lock", "person",
    "binary_sensor", "sensor", "scene", "script"
}

def is_ignored_entity(e: Dict[str, Any], advanced: bool) -> bool:
    eid = e.get("entity_id", "")
    dom = e.get("domain", "")
    name = norm(e.get("name", ""))

    if dom not in DEFAULT_ALLOWED_DOMAINS:
        return True

    if dom in {"update"}:
        return True

    if dom == "sensor":
        low = eid.lower()
        for suf in DEFAULT_IGNORE_ENTITY_ID_SUFFIXES:
            if low.endswith(suf):
                return True
        for needle in DEFAULT_IGNORE_ENTITY_ID_CONTAINS:
            if needle in low:
                return True
        for needle in ["rssi", "linkquality", "lqi", "snr", "signal", "uptime", "diagnostic", "debug"]:
            if needle in name:
                return True
        if (e.get("device_class") in DEFAULT_IGNORE_DEVICE_CLASSES) and not advanced:
            return True
        if not advanced:
            if not e.get("unit") and not e.get("device_class"):
                return True

    return False

def smart_filter_entities(entities: List[Dict[str, Any]], advanced: bool) -> List[Dict[str, Any]]:
    out = [e for e in entities if not is_ignored_entity(e, advanced=advanced)]
    sensors = [e for e in out if e["domain"] == "sensor"]
    if not advanced and len(sensors) > 24:
        def score(x: Dict[str, Any]) -> int:
            sc = 0
            if x.get("unit"): sc += 2
            if x.get("device_class"): sc += 2
            if x.get("state_class"): sc += 1
            return sc
        sensors_sorted = sorted(sensors, key=score, reverse=True)[:24]
        non = [e for e in out if e["domain"] != "sensor"]
        out = non + sensors_sorted
    return sorted(out, key=lambda x: norm(x.get("name") or x["entity_id"]))

# -------------------------
# Floor detection (Beneden/Boven)
# -------------------------
FLOOR_KEYWORDS = {
    "beneden": ["beneden", "begane grond", "bg", "downstairs", "ground floor", "vloer 0", "0e verdieping"],
    "boven": ["boven", "1e verdieping", "eerste verdieping", "2e verdieping", "upstairs", "floor 1", "floor 2"],
}

def guess_floor_for_area(area_name: str) -> Optional[str]:
    n = norm(area_name)
    for floor, keys in FLOOR_KEYWORDS.items():
        if any(k in n for k in keys):
            return floor
    return None

def guess_floor_for_entity_name(entity_name: str) -> Optional[str]:
    n = norm(entity_name)
    for floor, keys in FLOOR_KEYWORDS.items():
        if any(k in n for k in keys):
            return floor
    return None

# -------------------------
# Mushroom card helpers
# -------------------------
def _m_title(title: str, subtitle: str = "") -> Dict[str, Any]:
    card = {"type": "custom:mushroom-title-card", "title": title}
    if subtitle:
        card["subtitle"] = subtitle
    return card

def _m_chips(chips: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "custom:mushroom-chips-card", "chips": chips}

def _chip_entity(entity_id: str, icon: str = "", content_info: str = "name") -> Dict[str, Any]:
    c = {"type": "entity", "entity": entity_id, "content_info": content_info}
    if icon:
        c["icon"] = icon
    return c

def _chip_template(content: str, icon: str, tap_action: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = {"type": "template", "icon": icon, "content": content}
    if tap_action:
        c["tap_action"] = tap_action
    return c

def _grid(cards: List[Dict[str, Any]], columns_mobile: int = 2) -> Dict[str, Any]:
    return {"type": "grid", "columns": columns_mobile, "square": False, "cards": cards}

def _stack(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "vertical-stack", "cards": cards}

def card_for_entity(e: Dict[str, Any], advanced: bool) -> Optional[Dict[str, Any]]:
    eid = e["entity_id"]
    domain = e["domain"]

    if domain == "light":
        return {
            "type": "custom:mushroom-light-card",
            "entity": eid,
            "show_brightness_control": True,
            "use_light_color": True,
            "tap_action": {"action": "toggle"},
            "hold_action": {"action": "more-info"},
        }

    if domain == "switch":
        return {
            "type": "custom:mushroom-entity-card",
            "entity": eid,
            "tap_action": {"action": "toggle"},
            "hold_action": {"action": "more-info"},
        }

    if domain == "climate":
        return {
            "type": "custom:mushroom-climate-card",
            "entity": eid,
            "show_temperature_control": True,
            "hvac_modes": True,
        }

    if domain == "media_player":
        return {
            "type": "custom:mushroom-media-player-card",
            "entity": eid,
            "use_media_info": True,
            "show_volume_level": advanced,
            "volume_controls": ["volume_mute", "volume_set"] if advanced else ["volume_mute"],
            "media_controls": ["play_pause", "next", "previous"] if advanced else ["play_pause"],
        }

    if domain == "cover":
        return {
            "type": "custom:mushroom-cover-card",
            "entity": eid,
            "show_position_control": True,
            "show_buttons_control": True,
        }

    if domain == "lock":
        return {"type": "custom:mushroom-lock-card", "entity": eid, "tap_action": {"action": "toggle"}}

    if domain == "person":
        return {"type": "custom:mushroom-person-card", "entity": eid, "use_entity_picture": True}

    if domain in {"binary_sensor", "sensor"}:
        return {"type": "custom:mushroom-entity-card", "entity": eid, "tap_action": {"action": "more-info"}}

    # scenes/scripts are used only for "Nachtmodus" detection; not shown as cards here.
    return None

# -------------------------
# Grouping
# -------------------------
def group_entities_by_area(entities: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in entities:
        aid = e.get("area_id") or "_no_area_"
        groups.setdefault(aid, []).append(e)
    for aid in groups:
        groups[aid] = sorted(groups[aid], key=lambda x: norm(x.get("name") or x["entity_id"]))
    return groups

# -------------------------
# Top actions
# -------------------------
def build_top_actions_cards(
    all_entities: List[Dict[str, Any]],
    areas: List[Dict[str, Any]],
    grouped_by_area: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    area_by_id = {a.get("area_id"): a for a in areas if a.get("area_id")}
    lights_beneden: List[str] = []
    lights_boven: List[str] = []
    lights_all: List[str] = []

    for aid, ents in grouped_by_area.items():
        for e in ents:
            if e["domain"] != "light":
                continue
            eid = e["entity_id"]
            lights_all.append(eid)

            floor = None
            a = area_by_id.get(aid) if aid != "_no_area_" else None
            if a:
                floor = guess_floor_for_area(a.get("name") or "")
            if not floor:
                floor = guess_floor_for_entity_name(e.get("name") or "")
            if floor == "beneden":
                lights_beneden.append(eid)
            elif floor == "boven":
                lights_boven.append(eid)

    def btn(primary: str, icon: str, service: str, data: Dict[str, Any], secondary: str = "") -> Dict[str, Any]:
        return {
            "type": "custom:mushroom-template-card",
            "primary": primary,
            "secondary": secondary,
            "icon": icon,
            "tap_action": {"action": "call-service", "service": service, "data": data},
        }

    buttons: List[Dict[str, Any]] = []

    if lights_beneden:
        buttons.append(btn(
            "Alles uit (beneden)",
            "mdi:lightbulb-off-outline",
            "light.turn_off",
            {"entity_id": sorted(list(set(lights_beneden)))},
            "Zet alle beneden-lampen uit",
        ))
    if lights_boven:
        buttons.append(btn(
            "Alles uit (boven)",
            "mdi:lightbulb-off-outline",
            "light.turn_off",
            {"entity_id": sorted(list(set(lights_boven)))},
            "Zet alle boven-lampen uit",
        ))
    if lights_all:
        buttons.append(btn(
            "Alles uit",
            "mdi:power",
            "light.turn_off",
            {"entity_id": sorted(list(set(lights_all)))},
            "Zet alle lampen uit",
        ))

    night_scene = None
    night_script = None
    for e in all_entities:
        if e["entity_id"].startswith("scene.") and "nacht" in norm(e.get("name") or e["entity_id"]):
            night_scene = e["entity_id"]
            break
    for e in all_entities:
        if e["entity_id"].startswith("script.") and "nacht" in norm(e.get("name") or e["entity_id"]):
            night_script = e["entity_id"]
            break

    if night_scene:
        buttons.append(btn(
            "Nachtmodus",
            "mdi:weather-night",
            "scene.turn_on",
            {"entity_id": night_scene},
            "Activeer scene",
        ))
    elif night_script:
        buttons.append(btn(
            "Nachtmodus",
            "mdi:weather-night",
            "script.turn_on",
            {"entity_id": night_script},
            "Start script",
        ))
    else:
        buttons.append({
            "type": "custom:mushroom-template-card",
            "primary": "Nachtmodus",
            "secondary": "Tip: maak een Scene/Script met ‘nacht’ in de naam.",
            "icon": "mdi:weather-night",
            "tap_action": {"action": "more-info"},
        })

    return [
        _m_title("Top acties", "1-tap knoppen (voor iedereen te snappen)."),
        _grid(buttons[:6], columns_mobile=2),
    ]

# -------------------------
# Views
# -------------------------
def build_overview_view(
    all_entities: List[Dict[str, Any]],
    areas: List[Dict[str, Any]],
    grouped: Dict[str, List[Dict[str, Any]]],
    advanced: bool,
) -> Dict[str, Any]:
    chips: List[Dict[str, Any]] = []

    chips.append(_chip_template(
        "{{ states.light | selectattr('state','eq','on') | list | count }} aan",
        "mdi:lightbulb-group",
        tap_action={"action": "navigate", "navigation_path": "/lovelace/0"},
    ))

    for dom, icon in [("climate", "mdi:thermostat"), ("media_player", "mdi:play"), ("lock", "mdi:lock")]:
        for e in all_entities:
            if e["domain"] == dom:
                chips.append(_chip_entity(e["entity_id"], icon=icon, content_info="state"))
                break

    persons = [e for e in all_entities if e["domain"] == "person"][:3]
    for p in persons:
        chips.append(_chip_entity(p["entity_id"], content_info="name"))

    lights = [e for e in all_entities if e["domain"] == "light"][: (16 if advanced else 12)]
    climates = [e for e in all_entities if e["domain"] == "climate"][: (8 if advanced else 6)]
    media = [e for e in all_entities if e["domain"] == "media_player"][: (8 if advanced else 6)]
    covers = [e for e in all_entities if e["domain"] == "cover"][: (12 if advanced else 8)]

    cards: List[Dict[str, Any]] = [
        _m_title("Overzicht", "Simpel, strak, mobielvriendelijk."),
        _m_chips(chips),
    ]

    cards.extend(build_top_actions_cards(all_entities, areas, grouped))

    if lights:
        cards.append(_m_title("Lampen", "Tik = aan/uit. Ingedrukt = details."))
        cards.append(_grid([card_for_entity(e, advanced) for e in lights if card_for_entity(e, advanced)], columns_mobile=2))

    if climates:
        cards.append(_m_title("Klimaat"))
        cards.append(_stack([card_for_entity(e, advanced) for e in climates if card_for_entity(e, advanced)]))

    if media and advanced:
        cards.append(_m_title("Media"))
        cards.append(_stack([card_for_entity(e, advanced) for e in media if card_for_entity(e, advanced)]))

    if covers:
        cards.append(_m_title("Rolluiken / Gordijnen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in covers if card_for_entity(e, advanced)], columns_mobile=2))

    return {"title": "Overzicht", "path": "0", "icon": "mdi:view-dashboard", "cards": cards}

def build_area_view(area: Dict[str, Any], entities: List[Dict[str, Any]], advanced: bool) -> Dict[str, Any]:
    area_name = area.get("name") or "Ruimte"
    path = sanitize_filename(area_name)

    lights = [e for e in entities if e["domain"] == "light"]
    switches = [e for e in entities if e["domain"] == "switch"]
    climates = [e for e in entities if e["domain"] == "climate"]
    media = [e for e in entities if e["domain"] == "media_player"]
    covers = [e for e in entities if e["domain"] == "cover"]
    locks = [e for e in entities if e["domain"] == "lock"]
    binaries = [e for e in entities if e["domain"] == "binary_sensor"]
    sensors = [e for e in entities if e["domain"] == "sensor"]

    chips: List[Dict[str, Any]] = []
    for e in (lights[:4] + switches[:4]):
        chips.append(_chip_entity(e["entity_id"], content_info="name"))
    if climates[:1]:
        chips.append(_chip_entity(climates[0]["entity_id"], icon="mdi:thermostat", content_info="state"))
    if media[:1] and advanced:
        chips.append(_chip_entity(media[0]["entity_id"], icon="mdi:play", content_info="state"))

    cards: List[Dict[str, Any]] = [
        _m_title(area_name, "Alles van deze ruimte, overzichtelijk."),
    ]
    if chips:
        cards.append(_m_chips(chips))

    if lights:
        cards.append(_m_title("Lampen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in lights if card_for_entity(e, advanced)], columns_mobile=2))

    if switches and advanced:
        cards.append(_m_title("Schakelaars"))
        cards.append(_grid([card_for_entity(e, advanced) for e in switches if card_for_entity(e, advanced)], columns_mobile=2))

    if climates:
        cards.append(_m_title("Klimaat"))
        cards.append(_stack([card_for_entity(e, advanced) for e in climates if card_for_entity(e, advanced)]))

    if covers and advanced:
        cards.append(_m_title("Rolluiken / Gordijnen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in covers if card_for_entity(e, advanced)], columns_mobile=2))

    if media and advanced:
        cards.append(_m_title("Media"))
        cards.append(_stack([card_for_entity(e, advanced) for e in media if card_for_entity(e, advanced)]))

    if locks and advanced:
        cards.append(_m_title("Slot"))
        cards.append(_stack([card_for_entity(e, advanced) for e in locks if card_for_entity(e, advanced)]))

    if binaries:
        cards.append(_m_title("Status"))
        cards.append(_grid([card_for_entity(e, advanced) for e in binaries if card_for_entity(e, advanced)], columns_mobile=2))

    if sensors and advanced:
        cards.append(_m_title("Metingen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in sensors if card_for_entity(e, advanced)], columns_mobile=2))

    return {"title": area_name, "path": path, "icon": "mdi:home-outline", "cards": cards}

def build_no_area_view(entities: List[Dict[str, Any]], advanced: bool) -> Optional[Dict[str, Any]]:
    if not entities:
        return None
    cards: List[Dict[str, Any]] = [
        _m_title("Overig", "Entities zonder ruimte. Tip: geef ze een Area in HA."),
    ]
    cards_grid = [card_for_entity(e, advanced) for e in entities if card_for_entity(e, advanced)]
    if cards_grid:
        cards.append(_grid(cards_grid, columns_mobile=2))
    return {"title": "Overig", "path": "overig", "icon": "mdi:dots-horizontal", "cards": cards}

def build_floor_lights_view(
    floor_name: str,
    areas: List[Dict[str, Any]],
    grouped: Dict[str, List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    area_by_id = {a.get("area_id"): a for a in areas if a.get("area_id")}
    floor_lights: List[Dict[str, Any]] = []

    for aid, ents in grouped.items():
        a = area_by_id.get(aid) if aid != "_no_area_" else None
        area_floor = guess_floor_for_area(a.get("name") or "") if a else None

        for e in ents:
            if e["domain"] != "light":
                continue
            f = area_floor or guess_floor_for_entity_name(e.get("name") or "")
            if f == floor_name:
                floor_lights.append(e)

    floor_lights = sorted(floor_lights, key=lambda x: norm(x.get("name") or x["entity_id"]))
    if not floor_lights:
        return None

    title = "Lampen (Beneden)" if floor_name == "beneden" else "Lampen (Boven)"
    path = "lichten_beneden" if floor_name == "beneden" else "lichten_boven"
    icon = "mdi:stairs-down" if floor_name == "beneden" else "mdi:stairs-up"

    cards: List[Dict[str, Any]] = [
        _m_title(title, "Alle lampen bij elkaar — super handig."),
        _grid([card_for_entity(e, advanced=True) for e in floor_lights if card_for_entity(e, advanced=True)], columns_mobile=2),
    ]
    return {"title": title, "path": path, "icon": icon, "cards": cards}

# -------------------------
# Dashboard building
# -------------------------
def build_dashboard_yaml(
    dashboard_title: str,
    include_overig: bool = True,
    include_overview: bool = True,
    include_floor_light_tabs: bool = True,
    selected_area_ids: Optional[List[str]] = None,
    advanced: bool = False,
) -> Dict[str, Any]:
    raw_entities = build_entities_enriched()
    entities = smart_filter_entities(raw_entities, advanced=advanced)

    areas = get_area_registry()
    grouped = group_entities_by_area(entities)

    views: List[Dict[str, Any]] = []

    if include_overview:
        views.append(build_overview_view(entities, areas, grouped, advanced=advanced))

    if include_floor_light_tabs:
        v1 = build_floor_lights_view("beneden", areas, grouped)
        v2 = build_floor_lights_view("boven", areas, grouped)
        if v1:
            views.append(v1)
        if v2:
            views.append(v2)

    ordered_areas = sorted([a for a in areas if a.get("area_id")], key=lambda x: norm(x.get("name") or ""))

    for a in ordered_areas:
        aid = a.get("area_id")
        if selected_area_ids and aid not in selected_area_ids:
            continue
        ents = grouped.get(aid, [])
        if not ents:
            continue
        views.append(build_area_view(a, ents, advanced=advanced))

    if include_overig:
        v = build_no_area_view(grouped.get("_no_area_", []), advanced=advanced)
        if v:
            views.append(v)

    return {"title": dashboard_title, "views": views}

def build_configuration_snippet(dashboard_file: str, title: str) -> str:
    dash_slug = sanitize_filename(title)
    snippet = """
lovelace:
  mode: storage

  dashboards:
    {dash_slug}:
      mode: yaml
      title: "{title}"
      icon: mdi:view-dashboard
      show_in_sidebar: true
      filename: dashboards/{dashboard_file}
""".format(dash_slug=dash_slug, title=title.replace('"', '\\"'), dashboard_file=dashboard_file)
    return snippet.strip() + "\n"

# -------------------------
# Web UI (wizard) - IMPORTANT: NOT an f-string
# -------------------------
@app.route("/")
def index():
    html = """<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{APP_NAME}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-br from-slate-50 to-indigo-50 min-h-screen p-4">
  <div class="max-w-6xl mx-auto">
    <div class="bg-white rounded-2xl shadow-2xl p-6 sm:p-8 mb-6">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
        <div>
          <h1 class="text-3xl sm:text-4xl font-bold text-indigo-900">🧩 {APP_NAME}</h1>
          <p class="text-gray-600 mt-2">Maak automatisch professionele Mushroom dashboards: <b>Simpel</b> + <b>Uitgebreid</b>.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">{APP_VERSION}</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinding maken...</span>
          </div>
          <div class="flex gap-2 flex-wrap">
            <button onclick="reloadLovelace()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🔄 Reload Lovelace
            </button>
            <button onclick="openDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🧾 Debug HA
            </button>
          </div>
        </div>
      </div>

      <div id="tokenWarning" class="hidden mb-6 bg-yellow-50 border-l-4 border-yellow-400 p-4 rounded">
        <div class="flex">
          <div class="flex-shrink-0">⚠️</div>
          <div class="ml-3">
            <p class="text-sm text-yellow-700">
              <strong>Token ontbreekt!</strong> Preview werkt, maar opslaan/reload kan beperkt zijn.
            </p>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- LEFT -->
        <div>
          <div class="mb-4">
            <label class="block text-base font-semibold text-gray-700 mb-2">📝 Basisnaam</label>
            <input type="text" id="dashName" placeholder="bijv. Thuis"
                   class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none">
            <p class="text-xs text-gray-500 mt-1">
              Er worden 2 dashboards opgeslagen: <b>&lt;naam&gt; Simpel</b> en <b>&lt;naam&gt; Uitgebreid</b>.
            </p>
          </div>

          <div class="mb-4 bg-gray-50 border border-gray-200 p-4 rounded-xl">
            <div class="font-semibold text-gray-800 mb-3">⚙️ Opties</div>
            <label class="flex items-center gap-2 text-sm text-gray-700 mb-2">
              <input type="checkbox" id="optOverview" class="scale-110" checked>
              Overzicht tab toevoegen
            </label>
            <label class="flex items-center gap-2 text-sm text-gray-700 mb-2">
              <input type="checkbox" id="optOverig" class="scale-110" checked>
              “Overig” tab voor entities zonder ruimte
            </label>
            <label class="flex items-center gap-2 text-sm text-gray-700 mb-2">
              <input type="checkbox" id="optFloorTabs" class="scale-110" checked>
              “Licht per verdieping” tabs (Beneden/Boven)
            </label>
            <label class="flex items-center gap-2 text-sm text-gray-700">
              <input type="checkbox" id="optSelectAreas" class="scale-110" onchange="toggleAreaPicker()">
              Zelf ruimtes kiezen (anders: alle ruimtes)
            </label>
          </div>

          <div id="areasBox" class="mb-4 hidden bg-gray-50 border border-gray-200 p-4 rounded-xl">
            <div class="flex items-center justify-between mb-2">
              <div class="font-semibold text-gray-800">🏠 Ruimtes</div>
              <div class="flex gap-2">
                <button onclick="selectAllAreas()" class="text-xs bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">Select all</button>
                <button onclick="clearAllAreas()" class="text-xs bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">Clear</button>
              </div>
            </div>
            <div id="areasList" class="grid grid-cols-1 sm:grid-cols-2 gap-2"></div>
            <p class="text-xs text-gray-500 mt-2">Tip: noem areas met (Beneden/Boven) of “Begane grond / 1e verdieping”.</p>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
            <button onclick="previewDashboards()"
                    class="w-full bg-gray-900 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-black transition-all shadow-lg">
              👀 Preview (2 dashboards)
            </button>
            <button onclick="saveDashboards()"
                    class="w-full bg-gradient-to-r from-indigo-600 to-purple-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-indigo-700 hover:to-purple-700 transition-all shadow-lg">
              💾 Opslaan (2 dashboards)
            </button>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
            <button onclick="loadDashboards()"
                    class="w-full bg-white border border-gray-300 text-gray-800 py-3 px-4 rounded-xl text-lg font-semibold hover:bg-gray-100 transition-all shadow-lg">
              📋 Mijn Dashboards
            </button>
            <button onclick="copyConfigSnippet()"
                    class="w-full bg-amber-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-amber-700 transition-all shadow-lg">
              📎 Copy config snippets
            </button>
          </div>

        </div>

        <!-- RIGHT -->
        <div>
          <div class="bg-gray-50 p-6 rounded-xl border border-gray-200">
            <div class="flex items-center justify-between mb-3">
              <h3 class="text-xl font-bold text-gray-800">🧾 Preview YAML (Simpel)</h3>
              <button onclick="copyYaml('previewSimple')" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">📋 Copy</button>
            </div>
            <pre id="previewSimple" class="bg-gray-900 text-green-400 p-4 rounded-lg overflow-x-auto text-sm font-mono min-h-[200px]"></pre>
          </div>

          <div class="mt-4 bg-gray-50 p-6 rounded-xl border border-gray-200">
            <div class="flex items-center justify-between mb-3">
              <h3 class="text-xl font-bold text-gray-800">🧾 Preview YAML (Uitgebreid)</h3>
              <button onclick="copyYaml('previewAdvanced')" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">📋 Copy</button>
            </div>
            <pre id="previewAdvanced" class="bg-gray-900 text-green-400 p-4 rounded-lg overflow-x-auto text-sm font-mono min-h-[200px]"></pre>
          </div>

          <div class="mt-4 bg-white p-6 rounded-xl border border-gray-200">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-xl font-bold text-gray-800">🧩 configuration.yaml snippets</h3>
              <span class="text-xs px-2 py-1 rounded bg-gray-200 text-gray-700">Plak in config</span>
            </div>
            <pre id="configSnippet" class="bg-gray-50 p-4 rounded-lg overflow-x-auto text-sm font-mono min-h-[120px] text-gray-800"></pre>
          </div>
        </div>
      </div>
    </div>

    <div id="dashboardsList" class="bg-white rounded-2xl shadow-2xl p-8 hidden">
      <h2 class="text-2xl font-bold text-gray-800 mb-4">📚 Opgeslagen Dashboards</h2>
      <div id="dashboardsContent" class="space-y-3"></div>
    </div>
  </div>

<script>
  let areas = [];
  let selectedAreas = [];
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
    try {{
      const cfgRes = await fetch(API_BASE + '/api/config');
      const cfg = await cfgRes.json();
      if (!cfg.token_configured) document.getElementById('tokenWarning').classList.remove('hidden');

      const aRes = await fetch(API_BASE + '/api/areas');
      areas = await aRes.json();
      renderAreas();

      document.getElementById('previewSimple').textContent = '# Vul een naam in en klik Preview.';
      document.getElementById('previewAdvanced').textContent = '# Vul een naam in en klik Preview.';
      document.getElementById('configSnippet').textContent = '# Na preview/opslaan komt hier de snippets.';
      setStatus('Verbonden (' + areas.length + ' ruimtes)', 'green');
    }} catch (e) {{
      console.error(e);
      setStatus('Verbinding mislukt', 'red');
    }}
  }}

  function toggleAreaPicker() {{
    const on = document.getElementById('optSelectAreas').checked;
    const box = document.getElementById('areasBox');
    if (on) box.classList.remove('hidden'); else box.classList.add('hidden');
  }}

  function renderAreas() {{
    const box = document.getElementById('areasList');
    box.innerHTML = '';
    areas.forEach(a => {{
      const aid = a.area_id;
      const div = document.createElement('div');
      div.className = 'p-3 border-2 rounded-lg cursor-pointer hover:bg-indigo-50 hover:border-indigo-300 transition-all';
      const active = selectedAreas.includes(aid);
      div.classList.add(active ? 'bg-indigo-100' : 'bg-white');
      div.style.borderColor = active ? '#6366f1' : '#e5e7eb';

      div.innerHTML = '<div class="font-semibold text-sm">' + escapeHtml(a.name) + '</div>' +
                      '<div class="text-xs text-gray-500 font-mono">' + escapeHtml(aid) + '</div>';
      div.onclick = () => {{
        const i = selectedAreas.indexOf(aid);
        if (i > -1) selectedAreas.splice(i, 1);
        else selectedAreas.push(aid);
        renderAreas();
      }};
      box.appendChild(div);
    }});
  }}

  function selectAllAreas() {{
    selectedAreas = areas.map(a => a.area_id);
    renderAreas();
  }}

  function clearAllAreas() {{
    selectedAreas = [];
    renderAreas();
  }}

  function currentPayload() {{
    const base_title = document.getElementById('dashName').value.trim();
    const include_overview = document.getElementById('optOverview').checked;
    const include_overig = document.getElementById('optOverig').checked;
    const include_floor_tabs = document.getElementById('optFloorTabs').checked;
    const select_areas = document.getElementById('optSelectAreas').checked;
    const area_ids = select_areas ? selectedAreas : null;
    return {{ base_title, include_overview, include_overig, include_floor_tabs, area_ids }};
  }}

  async function previewDashboards() {{
    const p = currentPayload();
    if (!p.base_title) return alert('❌ Vul een basisnaam in!');
    const res = await fetch(API_BASE + '/api/preview_dashboards', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(p)
    }});
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Onbekende fout'));

    document.getElementById('previewSimple').textContent = data.simple_code || '—';
    document.getElementById('previewAdvanced').textContent = data.advanced_code || '—';
    document.getElementById('configSnippet').textContent = data.config_snippets || '# Snippets verschijnen na preview/opslaan.';
  }}

  async function saveDashboards() {{
    const p = currentPayload();
    if (!p.base_title) return alert('❌ Vul een basisnaam in!');
    const res = await fetch(API_BASE + '/api/create_dashboards', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(p)
    }});
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Onbekende fout'));

    document.getElementById('previewSimple').textContent = data.simple_code || '—';
    document.getElementById('previewAdvanced').textContent = data.advanced_code || '—';
    document.getElementById('configSnippet').textContent = data.config_snippets || '—';

    alert('✅ Opgeslagen:\\n- ' + data.simple_filename + '\\n- ' + data.advanced_filename +
          '\\n\\nPlak nu de snippets in configuration.yaml en herstart Home Assistant.');
  }}

  async function reloadLovelace() {{
    const res = await fetch(API_BASE + '/api/reload_lovelace', {{ method: 'POST' }});
    const data = await res.json();
    if (!res.ok || !data.ok) {{
      return alert('❌ Reload failed: ' + (data.error || 'Onbekend') + (data.details ? ('\\n\\n' + JSON.stringify(data.details)) : ''));
    }}
    alert('✅ Lovelace reload: ' + (data.result || 'OK'));
  }}

  async function loadDashboards() {{
    const response = await fetch(API_BASE + '/api/dashboards');
    const items = await response.json();

    const list = document.getElementById('dashboardsList');
    const content = document.getElementById('dashboardsContent');

    if (!items.length) {{
      list.classList.add('hidden');
      return alert('Nog geen dashboards opgeslagen!');
    }}

    list.classList.remove('hidden');

    let html = '';
    items.forEach(t => {{
      html += '<div class="bg-gray-50 border-2 border-gray-200 rounded-lg p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">';
      html += '<div><div class="font-semibold">' + escapeHtml(t.name) + '</div>';
      html += '<div class="text-sm text-gray-500 font-mono">' + escapeHtml(t.filename) + '</div></div>';
      html += '<div class="flex gap-2 flex-wrap">';
      html += '<button onclick="openDashboard(\\'' + t.filename + '\\')" class="bg-indigo-600 text-white px-4 py-2 rounded-lg hover:bg-indigo-700">📄 Open</button>';
      html += '<button onclick="downloadDashboard(\\'' + t.filename + '\\')" class="bg-white border border-gray-300 text-gray-800 px-4 py-2 rounded-lg hover:bg-gray-100">⬇️ Download</button>';
      html += '<button onclick="deleteDashboard(\\'' + t.filename + '\\')" class="bg-red-500 text-white px-4 py-2 rounded-lg hover:bg-red-600">🗑️ Verwijder</button>';
      html += '</div></div>';
    }});

    content.innerHTML = html;
    list.scrollIntoView({{ behavior: 'smooth' }});
  }}

  async function openDashboard(filename) {{
    const res = await fetch(API_BASE + '/api/dashboard?filename=' + encodeURIComponent(filename));
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Kon dashboard niet openen'));

    document.getElementById('previewSimple').textContent = data.code || '—';
    document.getElementById('previewAdvanced').textContent = '# Opened dashboard staat links.';
    document.getElementById('configSnippet').textContent = data.config_snippet || '—';
  }}

  async function deleteDashboard(filename) {{
    if (!confirm('Weet je zeker dat je ' + filename + ' wilt verwijderen?')) return;
    const response = await fetch(API_BASE + '/api/delete_dashboard', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ filename }})
    }});
    const result = await response.json();
    if (response.ok) {{
      alert('✅ Dashboard verwijderd!');
      loadDashboards();
    }} else {{
      alert('❌ Fout: ' + (result.error || 'Onbekende fout'));
    }}
  }}

  async function downloadDashboard(filename) {{
    window.open(API_BASE + '/api/download?filename=' + encodeURIComponent(filename), '_blank');
  }}

  function copyYaml(elId) {{
    const text = document.getElementById(elId).textContent || '';
    navigator.clipboard.writeText(text).then(() => alert('📋 YAML gekopieerd!'));
  }}

  function copyConfigSnippet() {{
    const text = document.getElementById('configSnippet').textContent || '';
    if (!text || text.startsWith('#')) return alert('Maak eerst een preview of sla op.');
    navigator.clipboard.writeText(text).then(() => alert('📋 Snippets gekopieerd!'));
  }}

  async function openDebug() {{
    const res = await fetch(API_BASE + '/api/debug/ha');
    const data = await res.json();
    alert(JSON.stringify(data, null, 2));
  }}

  init();
</script>
</body>
</html>
"""
    # fill only the two placeholders; all JS braces are already doubled where needed
    html = html.format(APP_NAME=APP_NAME, APP_VERSION=APP_VERSION)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# -------------------------
# API routes
# -------------------------
@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "token_configured": bool(SUPERVISOR_TOKEN),
        "dashboards_path": DASHBOARDS_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    })

@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "No token in container."}), 200
    try:
        r = ha_request("GET", "/api/", timeout=10)
        return jsonify({"ok": True, "status": r.status_code, "body": r.text[:400]}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

@app.route("/api/areas", methods=["GET"])
def api_areas():
    areas = get_area_registry()
    out = [{"area_id": a.get("area_id"), "name": a.get("name")} for a in areas if a.get("area_id")]
    return jsonify(out)

@app.route("/api/preview_dashboards", methods=["POST"])
def api_preview_dashboards():
    data = request.json or {}
    base_title = (data.get("base_title") or "Thuis").strip()
    include_overview = bool(data.get("include_overview", True))
    include_overig = bool(data.get("include_overig", True))
    include_floor_tabs = bool(data.get("include_floor_tabs", True))
    area_ids = data.get("area_ids")

    if not base_title:
        return jsonify({"error": "Basisnaam ontbreekt."}), 400

    simple_title = f"{base_title} Simpel"
    adv_title = f"{base_title} Uitgebreid"

    simple_dash = build_dashboard_yaml(
        dashboard_title=simple_title,
        include_overig=include_overig,
        include_overview=include_overview,
        include_floor_light_tabs=include_floor_tabs,
        selected_area_ids=area_ids if isinstance(area_ids, list) else None,
        advanced=False,
    )
    adv_dash = build_dashboard_yaml(
        dashboard_title=adv_title,
        include_overig=include_overig,
        include_overview=include_overview,
        include_floor_light_tabs=include_floor_tabs,
        selected_area_ids=area_ids if isinstance(area_ids, list) else None,
        advanced=True,
    )

    simple_code = safe_yaml_dump(simple_dash)
    adv_code = safe_yaml_dump(adv_dash)

    simple_file = f"{sanitize_filename(simple_title)}.yaml"
    adv_file = f"{sanitize_filename(adv_title)}.yaml"

    cfg_snips = (
        build_configuration_snippet(simple_file, simple_title)
        + "\n"
        + build_configuration_snippet(adv_file, adv_title)
    )

    return jsonify({
        "ok": True,
        "simple_code": simple_code,
        "advanced_code": adv_code,
        "config_snippets": cfg_snips,
    })

@app.route("/api/create_dashboards", methods=["POST"])
def api_create_dashboards():
    data = request.json or {}
    base_title = (data.get("base_title") or "Thuis").strip()
    include_overview = bool(data.get("include_overview", True))
    include_overig = bool(data.get("include_overig", True))
    include_floor_tabs = bool(data.get("include_floor_tabs", True))
    area_ids = data.get("area_ids")

    if not base_title:
        return jsonify({"error": "Basisnaam ontbreekt."}), 400

    simple_title = f"{base_title} Simpel"
    adv_title = f"{base_title} Uitgebreid"

    simple_dash = build_dashboard_yaml(
        dashboard_title=simple_title,
        include_overig=include_overig,
        include_overview=include_overview,
        include_floor_light_tabs=include_floor_tabs,
        selected_area_ids=area_ids if isinstance(area_ids, list) else None,
        advanced=False,
    )
    adv_dash = build_dashboard_yaml(
        dashboard_title=adv_title,
        include_overig=include_overig,
        include_overview=include_overview,
        include_floor_light_tabs=include_floor_tabs,
        selected_area_ids=area_ids if isinstance(area_ids, list) else None,
        advanced=True,
    )

    simple_code = safe_yaml_dump(simple_dash)
    adv_code = safe_yaml_dump(adv_dash)

    simple_fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(simple_title)}.yaml")
    adv_fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(adv_title)}.yaml")

    write_text_file(os.path.join(DASHBOARDS_PATH, simple_fn), simple_code)
    write_text_file(os.path.join(DASHBOARDS_PATH, adv_fn), adv_code)

    cfg_snips = (
        build_configuration_snippet(simple_fn, simple_title)
        + "\n"
        + build_configuration_snippet(adv_fn, adv_title)
    )

    return jsonify({
        "success": True,
        "simple_filename": simple_fn,
        "advanced_filename": adv_fn,
        "simple_code": simple_code,
        "advanced_code": adv_code,
        "config_snippets": cfg_snips,
    })

@app.route("/api/dashboards", methods=["GET"])
def api_dashboards():
    files = list_yaml_files(DASHBOARDS_PATH)
    return jsonify([{
        "filename": fn,
        "name": fn.replace(".yaml", "").replace("_", " ").title()
    } for fn in files])

@app.route("/api/dashboard", methods=["GET"])
def api_dashboard_read():
    filename = (request.args.get("filename", "") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(DASHBOARDS_PATH, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Bestand niet gevonden"}), 404
    content = read_text_file(filepath)
    title_guess = filename.replace(".yaml", "").replace("_", " ").title()
    cfg_snip = build_configuration_snippet(filename, title_guess)
    return jsonify({"filename": filename, "code": content, "title_guess": title_guess, "config_snippet": cfg_snip})

@app.route("/api/download", methods=["GET"])
def api_download():
    filename = (request.args.get("filename", "") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(DASHBOARDS_PATH, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Bestand niet gevonden"}), 404
    content = read_text_file(filepath)
    return Response(content, mimetype="text/yaml", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/api/delete_dashboard", methods=["POST"])
def api_delete_dashboard():
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not is_safe_filename(filename):
        return jsonify({"error": "Ongeldige filename"}), 400
    filepath = os.path.join(DASHBOARDS_PATH, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({"success": True})
    return jsonify({"error": "Bestand niet gevonden"}), 404

@app.route("/api/reload_lovelace", methods=["POST"])
def api_reload_lovelace():
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "Geen token in container."}), 400

    candidates = [
        ("lovelace", "reload", {}),
        ("homeassistant", "reload_core_config", {}),
    ]

    last = None
    for domain, service, payload in candidates:
        r, status = ha_call_service(domain, service, payload)
        if status == 200 and r.get("ok"):
            return jsonify({"ok": True, "result": f"{domain}.{service}"}), 200
        last = r

    return jsonify({"ok": False, "error": "Geen werkende lovelace reload service gevonden.", "details": last}), 400

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"{APP_NAME} starting... ({APP_VERSION})")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8099, debug=False)
