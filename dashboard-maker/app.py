#!/usr/bin/env python3
from __future__ import annotations

from flask import Flask, request, jsonify, Response
import os
import re
import json
import io
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

# ============================================================
# Dashboard Maker — “1000% werkt” edition
# - Works in HA add-on Ingress (no broken base path)
# - Reads supervisor_token from:
#   1) ENV SUPERVISOR_TOKEN (preferred)
#   2) /data/options.json -> supervisor_token (your add-on option)
#   3) /run/supervisor_token or /var/run/supervisor_token
# - Installs Mushroom (no HACS) + registers Lovelace resource
# - Installs a premium auto light/dark theme + tries to activate it
# - Generates WOW demo + 2 dashboards (Simpel/Uitgebreid)
# ============================================================

APP_VERSION = "2.4.0-1000pct-connect-ingress-optionsjson"
APP_NAME = "Dashboard Maker"

# --- Config paths ---
HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
DASHBOARDS_PATH = os.environ.get("DASHBOARDS_PATH") or os.path.join(HA_CONFIG_PATH, "dashboards")
THEMES_PATH = os.path.join(HA_CONFIG_PATH, "themes")
WWW_PATH = os.path.join(HA_CONFIG_PATH, "www")
WWW_COMMUNITY = os.path.join(WWW_PATH, "community")

# --- Mushroom install (no HACS) ---
MUSHROOM_VERSION = "3.3.0"
MUSHROOM_GITHUB_ZIP = f"https://github.com/piitaya/lovelace-mushroom/releases/download/v{MUSHROOM_VERSION}/mushroom.zip"
MUSHROOM_PATH = os.path.join(WWW_COMMUNITY, "mushroom")
MUSHROOM_RESOURCE_URL = "/local/community/mushroom/mushroom.js"

# --- Theme ---
THEME_NAME = "Dashboard Maker"
DASHBOARD_THEME_FILE = os.path.join(THEMES_PATH, "dashboard_maker.yaml")

# --- Flask ---
app = Flask(__name__)

# ============================================================
# Token discovery (robust)
# ============================================================
def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""

def _read_options_json_token() -> str:
    # Home Assistant add-ons: /data/options.json contains user options
    try:
        with open("/data/options.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        tok = (data.get("supervisor_token") or "").strip()
        return tok
    except Exception:
        return ""

def discover_token() -> str:
    # 1) Official supervisor token env
    tok = (os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()
    if tok:
        return tok

    # 2) User-provided add-on option (your config.yaml schema)
    tok = _read_options_json_token()
    if tok:
        return tok

    # 3) Alternative envs
    tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()
    if tok:
        return tok

    # 4) Supervisor token file paths
    for p in ("/var/run/supervisor_token", "/run/supervisor_token"):
        tok = _read_file(p)
        if tok:
            return tok

    return ""

SUPERVISOR_TOKEN = discover_token()

# Create dirs
Path(DASHBOARDS_PATH).mkdir(parents=True, exist_ok=True)
Path(THEMES_PATH).mkdir(parents=True, exist_ok=True)
Path(WWW_COMMUNITY).mkdir(parents=True, exist_ok=True)

print(f"== {APP_NAME} {APP_VERSION} ==")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Dashboards path: {DASHBOARDS_PATH}")
print(f"Token available: {bool(SUPERVISOR_TOKEN)}")
print(f"Mushroom path: {MUSHROOM_PATH}")

# ============================================================
# Helpers
# ============================================================
def sanitize_filename(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "_", name)
    return (name or "unnamed")[:80]

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

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
    os.makedirs(os.path.dirname(path), exist_ok=True)
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

# ============================================================
# Home Assistant API (Supervisor proxy)
# ============================================================
def ha_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}

def ha_request(method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
    # Supervisor proxy inside add-on network:
    url = f"http://supervisor/core{path}"
    return requests.request(method, url, headers=ha_headers(), json=json_body, timeout=timeout)

def ha_call_service(domain: str, service: str, data: dict | None = None) -> Tuple[Dict[str, Any], int]:
    if not SUPERVISOR_TOKEN:
        return {"ok": False, "error": "Geen token in container."}, 400
    try:
        resp = ha_request("POST", f"/api/services/{domain}/{service}", json_body=(data or {}), timeout=15)
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"{domain}.{service} failed ({resp.status_code})", "details": resp.text[:2000]}, 400
        try:
            return {"ok": True, "result": resp.json()}, 200
        except Exception:
            return {"ok": True, "result": resp.text}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

def ha_ping() -> Tuple[bool, str]:
    if not SUPERVISOR_TOKEN:
        return False, "Token ontbreekt"
    try:
        r = ha_request("GET", "/api/", timeout=10)
        if r.status_code == 200:
            return True, "OK"
        return False, f"HA /api/ status {r.status_code}"
    except Exception as e:
        return False, str(e)

# ============================================================
# Entities / Areas (with safe fallbacks)
# ============================================================
def get_states() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        # demo data
        return [
            {"entity_id": "light.woonkamer", "state": "off", "attributes": {"friendly_name": "Woonkamer Lamp"}},
            {"entity_id": "sensor.temp_woonkamer", "state": "21.1", "attributes": {"friendly_name": "Temperatuur", "unit_of_measurement": "°C", "device_class": "temperature"}},
            {"entity_id": "media_player.tv", "state": "off", "attributes": {"friendly_name": "TV"}},
        ]
    try:
        resp = ha_request("GET", "/api/states", timeout=12)
        if resp.status_code != 200:
            return []
        return resp.json()
    except Exception:
        return []

def get_area_registry() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        return [{"area_id": "woonkamer", "name": "Woonkamer (Beneden)"}, {"area_id": "slaapkamer", "name": "Slaapkamer (Boven)"}]
    try:
        resp = ha_request("GET", "/api/config/area_registry", timeout=12)
        if resp.status_code != 200:
            return []
        return resp.json()
    except Exception:
        return []

def get_entity_registry() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        return [{"entity_id": "light.woonkamer", "area_id": "woonkamer"}, {"entity_id": "sensor.temp_woonkamer", "area_id": "woonkamer"}]
    try:
        resp = ha_request("GET", "/api/config/entity_registry", timeout=12)
        if resp.status_code != 200:
            return []
        return resp.json()
    except Exception:
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

# ============================================================
# Smart filters (anti clutter)
# ============================================================
DEFAULT_IGNORE_ENTITY_ID_SUFFIXES = [
    "_rssi", "_linkquality", "_lqi", "_snr", "_signal",
    "_last_seen", "_lastseen", "_lastupdate",
    "_uptime", "_available", "_availability",
    "_battery", "_battery_level",
]
DEFAULT_IGNORE_ENTITY_ID_CONTAINS = [
    "linkquality", "rssi", "lqi", "snr", "signal", "last_seen", "lastseen", "uptime", "battery",
    "diagnostic", "debug", "heap", "stack", "watchdog",
]
DEFAULT_ALLOWED_DOMAINS = {"light", "switch", "climate", "media_player", "cover", "lock", "person", "binary_sensor", "sensor"}

def is_ignored_entity(e: Dict[str, Any], advanced: bool) -> bool:
    eid = (e.get("entity_id") or "")
    dom = (e.get("domain") or "")
    name = norm(e.get("name", ""))

    if dom not in DEFAULT_ALLOWED_DOMAINS:
        return True
    if dom in {"automation", "script", "scene", "update"}:
        return True

    if dom == "sensor":
        low = eid.lower()
        for suf in DEFAULT_IGNORE_ENTITY_ID_SUFFIXES:
            if low.endswith(suf):
                return True
        for needle in DEFAULT_IGNORE_ENTITY_ID_CONTAINS:
            if needle in low:
                return True
        for needle in ["rssi", "linkquality", "lqi", "snr", "signal", "uptime", "battery", "diagnostic", "debug"]:
            if needle in name:
                return True
        # Simpel dashboard: alleen "zinvolle" sensors
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

# ============================================================
# Floor detection (Beneden/Boven)
# ============================================================
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

# ============================================================
# Mushroom install + Lovelace resource
# ============================================================
def mushroom_installed() -> bool:
    try:
        return os.path.exists(os.path.join(MUSHROOM_PATH, "mushroom.js"))
    except Exception:
        return False

def download_and_extract_zip(url: str, target_dir: str):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        z.extractall(target_dir)

def install_mushroom() -> str:
    Path(WWW_COMMUNITY).mkdir(parents=True, exist_ok=True)

    if mushroom_installed():
        return "Mushroom: al geïnstalleerd"

    download_and_extract_zip(MUSHROOM_GITHUB_ZIP, WWW_COMMUNITY)

    if not mushroom_installed():
        raise RuntimeError("Mushroom installeren faalde: mushroom.js niet gevonden na extract.")

    return "Mushroom: geïnstalleerd"

def get_lovelace_resources() -> List[Dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        return []
    try:
        r = ha_request("GET", "/api/lovelace/resources", timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def ensure_mushroom_resource() -> str:
    if not SUPERVISOR_TOKEN:
        return "Resource: overgeslagen (geen token)"
    resources = get_lovelace_resources()
    if any((x.get("url") == MUSHROOM_RESOURCE_URL) for x in resources):
        return "Resource: al gekoppeld"

    payload = {"type": "module", "url": MUSHROOM_RESOURCE_URL}
    r = ha_request("POST", "/api/lovelace/resources", json_body=payload, timeout=15)

    # If HA returns "already exists" or similar, still fine
    if r.status_code in (200, 201):
        return "Resource: gekoppeld"
    return "Resource: best-effort (mogelijk al aanwezig)"

# ============================================================
# Theme “premium” (auto light/dark)
# ============================================================
THEME_PRESETS = {
    "indigo_luxe": {"label": "Indigo Luxe", "primary": "#6366f1", "accent": "#8b5cf6"},
    "emerald_fresh": {"label": "Emerald Fresh", "primary": "#10b981", "accent": "#34d399"},
    "amber_warm": {"label": "Amber Warm", "primary": "#f59e0b", "accent": "#f97316"},
    "rose_neon": {"label": "Rose Neon", "primary": "#f43f5e", "accent": "#fb7185"},
}

def build_theme_yaml(primary: str, accent: str, density: str = "comfy") -> str:
    if density not in ("comfy", "compact"):
        density = "comfy"
    radius = "18px" if density == "comfy" else "14px"
    shadow = "0 18px 40px rgba(0,0,0,0.14)" if density == "comfy" else "0 12px 26px rgba(0,0,0,0.14)"
    card_pad = "14px" if density == "comfy" else "10px"

    return f"""
{THEME_NAME}:
  primary-color: "{primary}"
  accent-color: "{accent}"

  font-family: "Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial"
  paper-font-common-base_-_font-family: "Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial"

  ha-card-border-radius: "{radius}"
  ha-card-box-shadow: "{shadow}"
  ha-card-background: "rgba(255,255,255,0.92)"

  card-mod-theme: "{THEME_NAME}"
  card-mod-card: |
    ha-card {{
      padding: {card_pad};
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      border: 1px solid rgba(15, 23, 42, 0.06);
    }}

  primary-background-color: "#f8fafc"
  secondary-background-color: "#eef2ff"
  app-header-background-color: "rgba(255,255,255,0.80)"
  app-header-text-color: "#0f172a"

  primary-text-color: "#0f172a"
  secondary-text-color: "rgba(15, 23, 42, 0.72)"
  divider-color: "rgba(15, 23, 42, 0.08)"

  paper-item-icon-color: "{primary}"
  paper-item-icon-active-color: "{accent}"
  paper-toggle-button-checked-button-color: "{primary}"
  paper-toggle-button-checked-bar-color: "{accent}"

  modes:
    dark:
      primary-background-color: "#0b1220"
      secondary-background-color: "#0f172a"
      ha-card-background: "rgba(2,6,23,0.86)"
      app-header-background-color: "rgba(2,6,23,0.66)"
      app-header-text-color: "#e5e7eb"
      primary-text-color: "#e5e7eb"
      secondary-text-color: "rgba(229, 231, 235, 0.72)"
      divider-color: "rgba(229, 231, 235, 0.08)"
      paper-item-icon-color: "{primary}"
      paper-item-icon-active-color: "{accent}"
""".strip() + "\n"

def install_dashboard_theme(preset_key: str, density: str) -> str:
    preset = THEME_PRESETS.get(preset_key) or THEME_PRESETS["indigo_luxe"]
    theme_yaml = build_theme_yaml(primary=preset["primary"], accent=preset["accent"], density=density)
    write_text_file(DASHBOARD_THEME_FILE, theme_yaml)
    return f"Theme: geïnstalleerd ({preset['label']})"

def ha_try_set_theme(theme_name: str, mode: str = "auto") -> Tuple[bool, str]:
    if not SUPERVISOR_TOKEN:
        return False, "Geen token"
    # reload themes first
    ha_call_service("frontend", "reload_themes", {})
    r, st = ha_call_service("frontend", "set_theme", {"name": theme_name, "mode": mode})
    if st == 200 and r.get("ok"):
        return True, "frontend.set_theme (mode)"
    r2, st2 = ha_call_service("frontend", "set_theme", {"name": theme_name})
    if st2 == 200 and r2.get("ok"):
        return True, "frontend.set_theme (fallback)"
    return False, "set_theme failed"

# ============================================================
# Mushroom card helpers
# ============================================================
def _m_title(title: str, subtitle: str = "") -> Dict[str, Any]:
    card = {"type": "custom:mushroom-title-card", "title": title}
    if subtitle:
        card["subtitle"] = subtitle
    return card

def _m_chips(chips: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "custom:mushroom-chips-card", "chips": chips}

def _chip_template(content: str, icon: str) -> Dict[str, Any]:
    return {"type": "template", "icon": icon, "content": content}

def _chip_entity(entity_id: str, icon: str = "", content_info: str = "name") -> Dict[str, Any]:
    c = {"type": "entity", "entity": entity_id, "content_info": content_info}
    if icon:
        c["icon"] = icon
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
    return None

# ============================================================
# Grouping / actions / views
# ============================================================
def group_entities_by_area(entities: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in entities:
        aid = e.get("area_id") or "_no_area_"
        groups.setdefault(aid, []).append(e)
    for aid in groups:
        groups[aid] = sorted(groups[aid], key=lambda x: norm(x.get("name") or x["entity_id"]))
    return groups

def build_top_actions_cards(all_entities: List[Dict[str, Any]], areas: List[Dict[str, Any]], grouped: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    area_by_id = {a.get("area_id"): a for a in areas if a.get("area_id")}
    lights_beneden: List[str] = []
    lights_boven: List[str] = []
    lights_all: List[str] = []

    for aid, ents in grouped.items():
        a = area_by_id.get(aid) if aid != "_no_area_" else None
        area_floor = guess_floor_for_area(a.get("name") or "") if a else None
        for e in ents:
            if e["domain"] != "light":
                continue
            eid = e["entity_id"]
            lights_all.append(eid)
            if area_floor == "beneden":
                lights_beneden.append(eid)
            elif area_floor == "boven":
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
        buttons.append(btn("Alles uit (beneden)", "mdi:lightbulb-off-outline", "light.turn_off",
                           {"entity_id": sorted(list(set(lights_beneden)))}, "Zet beneden-lampen uit"))
    if lights_boven:
        buttons.append(btn("Alles uit (boven)", "mdi:lightbulb-off-outline", "light.turn_off",
                           {"entity_id": sorted(list(set(lights_boven)))}, "Zet boven-lampen uit"))
    if lights_all:
        buttons.append(btn("Alles uit", "mdi:power", "light.turn_off",
                           {"entity_id": sorted(list(set(lights_all)))}, "Zet alle lampen uit"))

    return [
        _m_title("Top acties", "1 tik — iedereen snapt dit."),
        _grid(buttons[:6], columns_mobile=2),
    ]

def build_overview_view(all_entities: List[Dict[str, Any]], areas: List[Dict[str, Any]], grouped: Dict[str, List[Dict[str, Any]]], advanced: bool, density: str) -> Dict[str, Any]:
    columns = 2 if density == "comfy" else 3

    chips: List[Dict[str, Any]] = []
    chips.append(_chip_template("{{ states.light | selectattr('state','eq','on') | list | count }} aan", "mdi:lightbulb-group"))
    chips.append(_chip_template("{{ now().strftime('%H:%M') }}", "mdi:clock-outline"))

    for dom, icon in [("climate", "mdi:thermostat"), ("media_player", "mdi:play")]:
        for e in all_entities:
            if e["domain"] == dom:
                chips.append(_chip_entity(e["entity_id"], icon=icon, content_info="state"))
                break

    lights = [e for e in all_entities if e["domain"] == "light"][: (18 if advanced else 12)]
    climates = [e for e in all_entities if e["domain"] == "climate"][: (8 if advanced else 6)]
    media = [e for e in all_entities if e["domain"] == "media_player"][: (8 if advanced else 4)]
    covers = [e for e in all_entities if e["domain"] == "cover"][: (10 if advanced else 6)]

    cards: List[Dict[str, Any]] = [
        _m_title("Overzicht", "Premium look. Mobiel strak. Desktop ook."),
        _m_chips(chips),
    ]
    cards.extend(build_top_actions_cards(all_entities, areas, grouped))

    if lights:
        cards.append(_m_title("Lampen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in lights if card_for_entity(e, advanced)], columns_mobile=columns))

    if climates:
        cards.append(_m_title("Klimaat"))
        cards.append(_stack([card_for_entity(e, advanced) for e in climates if card_for_entity(e, advanced)]))

    if media and advanced:
        cards.append(_m_title("Media"))
        cards.append(_stack([card_for_entity(e, advanced) for e in media if card_for_entity(e, advanced)]))

    if covers:
        cards.append(_m_title("Rolluiken / Gordijnen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in covers if card_for_entity(e, advanced)], columns_mobile=columns))

    return {"title": "Overzicht", "path": "0", "icon": "mdi:view-dashboard", "cards": cards}

def build_area_view(area: Dict[str, Any], entities: List[Dict[str, Any]], advanced: bool, density: str) -> Dict[str, Any]:
    columns = 2 if density == "comfy" else 3
    area_name = area.get("name") or "Ruimte"
    path = sanitize_filename(area_name)

    lights = [e for e in entities if e["domain"] == "light"]
    switches = [e for e in entities if e["domain"] == "switch"]
    climates = [e for e in entities if e["domain"] == "climate"]
    media = [e for e in entities if e["domain"] == "media_player"]
    covers = [e for e in entities if e["domain"] == "cover"]
    binaries = [e for e in entities if e["domain"] == "binary_sensor"]
    sensors = [e for e in entities if e["domain"] == "sensor"]

    chips: List[Dict[str, Any]] = []
    for e in (lights[:4] + switches[:3]):
        chips.append(_chip_entity(e["entity_id"], content_info="name"))
    if climates[:1]:
        chips.append(_chip_entity(climates[0]["entity_id"], icon="mdi:thermostat", content_info="state"))

    cards: List[Dict[str, Any]] = [_m_title(area_name, "Alles van deze ruimte, overzichtelijk.")]
    if chips:
        cards.append(_m_chips(chips))

    if lights:
        cards.append(_m_title("Lampen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in lights if card_for_entity(e, advanced)], columns_mobile=columns))

    if switches and advanced:
        cards.append(_m_title("Schakelaars"))
        cards.append(_grid([card_for_entity(e, advanced) for e in switches if card_for_entity(e, advanced)], columns_mobile=columns))

    if climates:
        cards.append(_m_title("Klimaat"))
        cards.append(_stack([card_for_entity(e, advanced) for e in climates if card_for_entity(e, advanced)]))

    if covers and advanced:
        cards.append(_m_title("Covers"))
        cards.append(_grid([card_for_entity(e, advanced) for e in covers if card_for_entity(e, advanced)], columns_mobile=columns))

    if media and advanced:
        cards.append(_m_title("Media"))
        cards.append(_stack([card_for_entity(e, advanced) for e in media if card_for_entity(e, advanced)]))

    if binaries:
        cards.append(_m_title("Status"))
        cards.append(_grid([card_for_entity(e, advanced) for e in binaries if card_for_entity(e, advanced)], columns_mobile=columns))

    if sensors and advanced:
        cards.append(_m_title("Metingen"))
        cards.append(_grid([card_for_entity(e, advanced) for e in sensors if card_for_entity(e, advanced)], columns_mobile=columns))

    return {"title": area_name, "path": path, "icon": "mdi:home-outline", "cards": cards}

def build_no_area_view(entities: List[Dict[str, Any]], advanced: bool, density: str) -> Optional[Dict[str, Any]]:
    if not entities:
        return None
    columns = 2 if density == "comfy" else 3
    cards: List[Dict[str, Any]] = [
        _m_title("Overig", "Dingen zonder ruimte. Tip: geef ze een ruimte in Home Assistant."),
        _grid([card_for_entity(e, advanced) for e in entities if card_for_entity(e, advanced)], columns_mobile=columns),
    ]
    return {"title": "Overig", "path": "overig", "icon": "mdi:dots-horizontal", "cards": cards}

def build_floor_lights_view(floor_name: str, entities: List[Dict[str, Any]], areas: List[Dict[str, Any]], grouped: Dict[str, List[Dict[str, Any]]], density: str) -> Optional[Dict[str, Any]]:
    area_by_id = {a.get("area_id"): a for a in areas if a.get("area_id")}
    floor_lights: List[Dict[str, Any]] = []

    for aid, ents in grouped.items():
        a = area_by_id.get(aid) if aid != "_no_area_" else None
        area_floor = guess_floor_for_area(a.get("name") or "") if a else None
        for e in ents:
            if e["domain"] != "light":
                continue
            if area_floor == floor_name:
                floor_lights.append(e)

    if not floor_lights:
        return None

    columns = 2 if density == "comfy" else 3
    floor_lights = sorted(floor_lights, key=lambda x: norm(x.get("name") or x["entity_id"]))
    title = "Lampen (Beneden)" if floor_name == "beneden" else "Lampen (Boven)"
    path = "lichten_beneden" if floor_name == "beneden" else "lichten_boven"
    icon = "mdi:stairs-down" if floor_name == "beneden" else "mdi:stairs-up"

    cards: List[Dict[str, Any]] = [
        _m_title(title, "Alle lampen bij elkaar — super handig."),
        _grid([card_for_entity(e, advanced=True) for e in floor_lights if card_for_entity(e, advanced=True)], columns_mobile=columns),
    ]
    return {"title": title, "path": path, "icon": icon, "cards": cards}

def build_dashboard_yaml(
    dashboard_title: str,
    include_overig: bool = True,
    include_overview: bool = True,
    include_floor_light_tabs: bool = True,
    selected_area_ids: Optional[List[str]] = None,
    advanced: bool = False,
    density: str = "comfy",
) -> Dict[str, Any]:
    raw_entities = build_entities_enriched()
    entities = smart_filter_entities(raw_entities, advanced=advanced)

    areas = get_area_registry()
    grouped = group_entities_by_area(entities)

    views: List[Dict[str, Any]] = []

    if include_overview:
        views.append(build_overview_view(entities, areas, grouped, advanced=advanced, density=density))

    if include_floor_light_tabs:
        v1 = build_floor_lights_view("beneden", entities, areas, grouped, density=density)
        v2 = build_floor_lights_view("boven", entities, areas, grouped, density=density)
        if v1: views.append(v1)
        if v2: views.append(v2)

    ordered_areas = sorted([a for a in areas if a.get("area_id")], key=lambda x: norm(x.get("name") or ""))
    for a in ordered_areas:
        aid = a.get("area_id")
        if selected_area_ids and aid not in selected_area_ids:
            continue
        ents = grouped.get(aid, [])
        if not ents:
            continue
        views.append(build_area_view(a, ents, advanced=advanced, density=density))

    if include_overig:
        v = build_no_area_view(grouped.get("_no_area_", []), advanced=advanced, density=density)
        if v:
            views.append(v)

    return {"title": dashboard_title, "views": views}

# ============================================================
# Wizard UI (Ingress-safe)
# IMPORTANT: API_BASE must be '' (relative paths) for Ingress.
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__APP_NAME__</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-br from-slate-50 to-indigo-50 min-h-screen p-4">
  <div class="max-w-5xl mx-auto">
    <div class="bg-white rounded-2xl shadow-2xl p-6 sm:p-8 mb-6">

      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
        <div>
          <h1 class="text-3xl sm:text-4xl font-bold text-indigo-900">🧩 __APP_NAME__</h1>
          <p class="text-gray-600 mt-2">Klik, kies stijl, klaar. Professionele dashboards — zonder technische kennis.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">__APP_VERSION__</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinden…</span>
          </div>
          <div class="flex gap-2 flex-wrap">
            <button onclick="reloadDashboards()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🔄 Vernieuwen
            </button>
            <button onclick="openDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🧾 Debug
            </button>
          </div>
        </div>
      </div>

      <div class="bg-slate-50 border border-slate-200 rounded-2xl p-4 mb-6">
        <div class="flex items-center justify-between text-sm font-semibold">
          <div id="step1Dot" class="flex items-center gap-2"><span class="w-3 h-3 rounded-full bg-indigo-500 inline-block"></span> Stap 1</div>
          <div class="flex-1 mx-3 h-1 bg-slate-200 rounded"></div>
          <div id="step2Dot" class="flex items-center gap-2 text-slate-500"><span class="w-3 h-3 rounded-full bg-slate-300 inline-block"></span> Stap 2</div>
          <div class="flex-1 mx-3 h-1 bg-slate-200 rounded"></div>
          <div id="step3Dot" class="flex items-center gap-2 text-slate-500"><span class="w-3 h-3 rounded-full bg-slate-300 inline-block"></span> Stap 3</div>
          <div class="flex-1 mx-3 h-1 bg-slate-200 rounded"></div>
          <div id="step4Dot" class="flex items-center gap-2 text-slate-500"><span class="w-3 h-3 rounded-full bg-slate-300 inline-block"></span> Klaar</div>
        </div>
      </div>

      <div id="step1" class="border border-slate-200 rounded-2xl p-5">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">Stap 1 — Super setup (automatisch)</h2>
            <p class="text-slate-600 mt-1">We installeren Mushroom + zetten een premium auto licht/donker stijl aan.</p>
          </div>
          <div class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-700">1 klik</div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Stijl</div>
            <select id="preset" class="mt-2 w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-indigo-500 focus:outline-none">
              <option value="indigo_luxe">Indigo Luxe</option>
              <option value="emerald_fresh">Emerald Fresh</option>
              <option value="amber_warm">Amber Warm</option>
              <option value="rose_neon">Rose Neon</option>
            </select>
            <p class="text-xs text-slate-500 mt-2">Kies een vibe. Alles wordt meteen strak.</p>
          </div>

          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Layout</div>
            <select id="density" class="mt-2 w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-indigo-500 focus:outline-none">
              <option value="comfy">Comfy (luchtig)</option>
              <option value="compact">Compact (minder scroll)</option>
            </select>
            <p class="text-xs text-slate-500 mt-2">Compact = meer op 1 scherm (mobiel).</p>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Home Assistant</div>
            <div id="chkEngine" class="text-sm mt-1 text-slate-500">⏳ controleren…</div>
          </div>
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Mushroom</div>
            <div id="chkCards" class="text-sm mt-1 text-slate-500">⏳ wachten…</div>
          </div>
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Theme</div>
            <div id="chkStyle" class="text-sm mt-1 text-slate-500">⏳ wachten…</div>
          </div>
        </div>

        <div class="mt-4 flex flex-col sm:flex-row gap-3">
          <button onclick="runSetup()" class="w-full sm:w-auto bg-gradient-to-r from-indigo-600 to-purple-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-indigo-700 hover:to-purple-700 shadow-lg">
            🚀 Alles automatisch instellen
          </button>
          <div class="text-sm text-slate-500 flex items-center">
            <span id="setupHint">Klik één keer. Wij doen de rest.</span>
          </div>
        </div>
      </div>

      <div id="step2" class="border border-slate-200 rounded-2xl p-5 mt-4 opacity-50 pointer-events-none">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">Stap 2 — WOW demo</h2>
            <p class="text-slate-600 mt-1">Maak een voorbeeld dashboard om direct te zien hoe ziek dit is.</p>
          </div>
          <div class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-700">1 klik</div>
        </div>
        <div class="mt-4 flex flex-col sm:flex-row gap-3">
          <button onclick="createDemo()" class="w-full sm:w-auto bg-slate-900 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-black shadow-lg">
            ✨ Maak demo dashboard
          </button>
          <div class="text-sm text-slate-500 flex items-center">
            <span>Je kunt dit later altijd verwijderen.</span>
          </div>
        </div>
      </div>

      <div id="step3" class="border border-slate-200 rounded-2xl p-5 mt-4 opacity-50 pointer-events-none">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">Stap 3 — Maak jouw dashboards</h2>
            <p class="text-slate-600 mt-1">Geef een naam. Wij maken automatisch 2 dashboards: <b>Simpel</b> & <b>Uitgebreid</b>.</p>
          </div>
          <div class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-700">Nieuw</div>
        </div>

        <div class="mt-4">
          <label class="block text-base font-semibold text-gray-700 mb-2">Naam</label>
          <input type="text" id="dashName" placeholder="bijv. Thuis"
                 class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none">

          <div class="mt-3 flex flex-col sm:flex-row gap-3">
            <button onclick="createMine()" class="w-full sm:w-auto bg-gradient-to-r from-indigo-600 to-purple-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-indigo-700 hover:to-purple-700 shadow-lg">
              🎨 Maak mijn dashboards
            </button>
          </div>
        </div>
      </div>

      <div id="step4" class="border border-slate-200 rounded-2xl p-5 mt-4 hidden">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">🎉 Klaar!</h2>
            <p class="text-slate-600 mt-1">Je dashboards zijn opgeslagen. Toon ze hieronder.</p>
          </div>
          <div class="text-xs px-2 py-1 rounded bg-green-100 text-green-700">Gereed</div>
        </div>

        <div class="mt-4 flex flex-col sm:flex-row gap-3">
          <button onclick="loadDashboards()" class="w-full sm:w-auto bg-slate-900 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-black shadow-lg">
            📋 Toon mijn dashboards
          </button>
        </div>
      </div>

    </div>

    <div id="dashboardsList" class="bg-white rounded-2xl shadow-2xl p-6 sm:p-8 hidden">
      <h2 class="text-2xl font-bold text-gray-800 mb-4">📚 Dashboards</h2>
      <div id="dashboardsContent" class="space-y-3"></div>
    </div>
  </div>

<script>
  // IMPORTANT FOR INGRESS:
  // Use relative paths only. Do NOT build API_BASE from pathname.
  const API_BASE = '';

  function setStatus(text, color = 'gray') {
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }

  function escapeHtml(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function setCheck(id, ok, msg) {
    const el = document.getElementById(id);
    el.textContent = (ok ? '✅ ' : '❌ ') + msg;
    el.className = 'text-sm mt-1 ' + (ok ? 'text-green-700' : 'text-red-700');
  }

  function unlockStep(stepId) {
    const el = document.getElementById(stepId);
    el.classList.remove('opacity-50', 'pointer-events-none');
  }

  function showStep4() {
    document.getElementById('step4').classList.remove('hidden');
  }

  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      const cfgRes = await fetch(API_BASE + '/api/config', { cache: 'no-store' });
      if (!cfgRes.ok) throw new Error('API /api/config niet bereikbaar');
      const cfg = await cfgRes.json();

      if (cfg.ha_ok) {
        setCheck('chkEngine', true, 'HA: verbonden');
      } else {
        setCheck('chkEngine', false, 'HA: niet bereikbaar');
      }

      setCheck('chkCards', true, cfg.mushroom_installed ? 'Mushroom: aanwezig' : 'Mushroom: klaar om te installeren');
      setCheck('chkStyle', true, cfg.theme_file_exists ? 'Theme: aanwezig' : 'Theme: klaar om te installeren');

      if (!cfg.token_configured) {
        setStatus('Verbonden (zonder token)', 'yellow');
        document.getElementById('setupHint').textContent =
          'Token ontbreekt. Zet supervisor_token in add-on opties.';
      } else {
        setStatus('Verbonden', 'green');
      }

      unlockStep('step1');
      // user must run setup to unlock next steps
    } catch (e) {
      console.error(e);
      setStatus('Verbinding mislukt', 'red');
      setCheck('chkEngine', false, 'Kan UI API niet bereiken');
      setCheck('chkCards', false, 'Kan UI API niet bereiken');
      setCheck('chkStyle', false, 'Kan UI API niet bereiken');
    }
  }

  async function runSetup() {
    const preset = document.getElementById('preset').value;
    const density = document.getElementById('density').value;

    document.getElementById('setupHint').textContent = 'Bezig… (Mushroom + resource + theme + auto licht/donker)';
    setCheck('chkCards', true, 'Bezig…');
    setCheck('chkStyle', true, 'Bezig…');

    try {
      const res = await fetch(API_BASE + '/api/setup', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ preset, density })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        document.getElementById('setupHint').textContent = 'Dit lukte niet. Probeer opnieuw.';
        return alert('❌ Setup mislukt: ' + (data.error || 'Onbekend') + (data.details ? ('\n\n' + data.details) : ''));
      }

      setCheck('chkCards', true, 'Klaar');
      setCheck('chkStyle', true, 'Klaar');
      document.getElementById('setupHint').textContent = 'Klaar! Je kunt verder.';

      unlockStep('step2');
      unlockStep('step3');

      alert('✅ Setup klaar!\n\n' + (data.steps ? data.steps.join('\n') : ''));
    } catch (e) {
      console.error(e);
      document.getElementById('setupHint').textContent = 'Dit lukte niet. Probeer opnieuw.';
      alert('❌ Setup mislukt.');
    }
  }

  async function createDemo() {
    try {
      const density = document.getElementById('density').value;
      const res = await fetch(API_BASE + '/api/create_demo', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ density })
      });
      const data = await res.json();
      if (!res.ok || !data.success) return alert('❌ Demo mislukt: ' + (data.error || 'Onbekend'));
      alert('✅ Demo gemaakt: ' + data.filename);
      showStep4();
    } catch (e) {
      console.error(e);
      alert('❌ Demo mislukt.');
    }
  }

  async function createMine() {
    const base_title = document.getElementById('dashName').value.trim();
    if (!base_title) return alert('❌ Vul een naam in.');

    const density = document.getElementById('density').value;

    const payload = {
      base_title,
      include_overview: true,
      include_overig: true,
      include_floor_tabs: true,
      area_ids: null,
      density
    };

    try {
      const res = await fetch(API_BASE + '/api/create_dashboards', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok || !data.success) return alert('❌ Maken mislukt: ' + (data.error || 'Onbekend'));

      await reloadDashboards();
      alert('✅ Klaar!\n- ' + data.simple_filename + '\n- ' + data.advanced_filename);
      showStep4();
    } catch (e) {
      console.error(e);
      alert('❌ Maken mislukt.');
    }
  }

  async function reloadDashboards() {
    try { await fetch(API_BASE + '/api/reload_lovelace', { method: 'POST' }); } catch (e) {}
  }

  async function loadDashboards() {
    const response = await fetch(API_BASE + '/api/dashboards', { cache: 'no-store' });
    const items = await response.json();

    const list = document.getElementById('dashboardsList');
    const content = document.getElementById('dashboardsContent');

    if (!items.length) {
      list.classList.add('hidden');
      return alert('Nog geen dashboards opgeslagen!');
    }

    list.classList.remove('hidden');

    let html = '';
    items.forEach(t => {
      html += '<div class="bg-slate-50 border-2 border-slate-200 rounded-xl p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">';
      html += '<div><div class="font-semibold">' + escapeHtml(t.name) + '</div>';
      html += '<div class="text-sm text-slate-500 font-mono">' + escapeHtml(t.filename) + '</div></div>';
      html += '<div class="flex gap-2 flex-wrap">';
      html += '<button onclick="downloadDashboard(\\'' + t.filename + '\\')" class="bg-white border border-gray-300 text-gray-800 px-4 py-2 rounded-lg hover:bg-gray-100">⬇️ Download</button>';
      html += '<button onclick="deleteDashboard(\\'' + t.filename + '\\')" class="bg-red-500 text-white px-4 py-2 rounded-lg hover:bg-red-600">🗑️ Verwijder</button>';
      html += '</div></div>';
    });

    content.innerHTML = html;
    list.scrollIntoView({ behavior: 'smooth' });
  }

  async function deleteDashboard(filename) {
    if (!confirm('Weet je zeker dat je dit dashboard wilt verwijderen?')) return;
    const response = await fetch(API_BASE + '/api/delete_dashboard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename })
    });
    const result = await response.json();
    if (response.ok) {
      alert('✅ Verwijderd!');
      loadDashboards();
    } else {
      alert('❌ Fout: ' + (result.error || 'Onbekende fout'));
    }
  }

  async function downloadDashboard(filename) {
    window.open(API_BASE + '/api/download?filename=' + encodeURIComponent(filename), '_blank');
  }

  async function openDebug() {
    const res = await fetch(API_BASE + '/api/debug/ha', { cache: 'no-store' });
    const data = await res.json();
    alert(JSON.stringify(data, null, 2));
  }

  init();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    html = HTML_PAGE.replace("__APP_NAME__", APP_NAME).replace("__APP_VERSION__", APP_VERSION)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# ============================================================
# API routes
# ============================================================
@app.route("/api/config", methods=["GET"])
def api_config():
    ok, msg = ha_ping()
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "token_configured": bool(SUPERVISOR_TOKEN),
        "dashboards_path": DASHBOARDS_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "mushroom_installed": mushroom_installed(),
        "theme_file_exists": os.path.exists(DASHBOARD_THEME_FILE),
        "ha_ok": ok,
        "ha_msg": msg,
    })

@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    info = {
        "token_configured": bool(SUPERVISOR_TOKEN),
        "token_source_hint": "env SUPERVISOR_TOKEN OR /data/options.json supervisor_token OR /run/supervisor_token",
    }
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "No token in container.", "info": info}), 200
    try:
        r = ha_request("GET", "/api/", timeout=10)
        return jsonify({"ok": (r.status_code == 200), "status": r.status_code, "body": r.text[:400], "info": info}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "info": info}), 200

@app.route("/api/setup", methods=["POST"])
def api_setup():
    if not SUPERVISOR_TOKEN:
        return jsonify({"ok": False, "error": "Geen token. Zet supervisor_token in add-on opties."}), 400

    data = request.json or {}
    preset = (data.get("preset") or "indigo_luxe").strip()
    density = (data.get("density") or "comfy").strip()

    steps: List[str] = []
    try:
        steps.append(install_mushroom())
        steps.append(ensure_mushroom_resource())
        steps.append(install_dashboard_theme(preset, density))

        # Reload themes + set theme in auto mode
        ok_theme, how = ha_try_set_theme(THEME_NAME, mode="auto")
        if ok_theme:
            steps.append(f"Theme actief (auto licht/donker) [{how}]")
        else:
            steps.append("Theme geïnstalleerd (activeren niet gelukt, maar staat klaar)")

        # Reload lovelace
        ha_call_service("lovelace", "reload", {})
        steps.append("Lovelace vernieuwd")

        return jsonify({"ok": True, "steps": steps}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps}), 500

@app.route("/api/create_demo", methods=["POST"])
def api_create_demo():
    data = request.json or {}
    density = (data.get("density") or "comfy").strip()

    title = "WOW Demo Dashboard"
    dash = build_dashboard_yaml(
        dashboard_title=title,
        include_overig=True,
        include_overview=True,
        include_floor_light_tabs=True,
        selected_area_ids=None,
        advanced=True,
        density=density,
    )
    code = safe_yaml_dump(dash)
    fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(title)}.yaml")
    write_text_file(os.path.join(DASHBOARDS_PATH, fn), code)
    return jsonify({"success": True, "filename": fn}), 200

@app.route("/api/create_dashboards", methods=["POST"])
def api_create_dashboards():
    data = request.json or {}
    base_title = (data.get("base_title") or "Thuis").strip()
    include_overview = bool(data.get("include_overview", True))
    include_overig = bool(data.get("include_overig", True))
    include_floor_tabs = bool(data.get("include_floor_tabs", True))
    area_ids = data.get("area_ids")
    density = (data.get("density") or "comfy").strip()

    if not base_title:
        return jsonify({"success": False, "error": "Naam ontbreekt."}), 400

    simple_title = f"{base_title} Simpel"
    adv_title = f"{base_title} Uitgebreid"

    simple_dash = build_dashboard_yaml(
        dashboard_title=simple_title,
        include_overig=include_overig,
        include_overview=include_overview,
        include_floor_light_tabs=include_floor_tabs,
        selected_area_ids=area_ids if isinstance(area_ids, list) else None,
        advanced=False,
        density=density,
    )
    adv_dash = build_dashboard_yaml(
        dashboard_title=adv_title,
        include_overig=include_overig,
        include_overview=include_overview,
        include_floor_light_tabs=include_floor_tabs,
        selected_area_ids=area_ids if isinstance(area_ids, list) else None,
        advanced=True,
        density=density,
    )

    simple_code = safe_yaml_dump(simple_dash)
    adv_code = safe_yaml_dump(adv_dash)

    simple_fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(simple_title)}.yaml")
    adv_fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(adv_title)}.yaml")

    write_text_file(os.path.join(DASHBOARDS_PATH, simple_fn), simple_code)
    write_text_file(os.path.join(DASHBOARDS_PATH, adv_fn), adv_code)

    return jsonify({
        "success": True,
        "simple_filename": simple_fn,
        "advanced_filename": adv_fn,
    }), 200

@app.route("/api/dashboards", methods=["GET"])
def api_dashboards():
    files = list_yaml_files(DASHBOARDS_PATH)
    return jsonify([{"filename": fn, "name": fn.replace(".yaml", "").replace("_", " ").title()} for fn in files])

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

    return jsonify({"ok": False, "error": "Vernieuwen lukt niet.", "details": last}), 400

# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"{APP_NAME} starting... ({APP_VERSION})")
    print("=" * 60)
    # Re-discover token at boot in case options.json changes between builds
    SUPERVISOR_TOKEN = discover_token()
    app.run(host="0.0.0.0", port=8099, debug=False)
