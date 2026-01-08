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
import zipfile
import io
import json

APP_VERSION = "2.4.0-connection-fixed-llat+supervisor-fallback"
APP_NAME = "Dashboard Maker"

app = Flask(__name__)

# -------------------------
# Paths
# -------------------------
HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
DASHBOARDS_PATH = os.environ.get("DASHBOARDS_PATH") or os.path.join(HA_CONFIG_PATH, "dashboards")

# Add-on options.json path (Supervisor injects this into add-on containers)
# Typical location: /data/options.json
ADDON_OPTIONS_PATH = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")

# -------------------------
# Mushroom install (no HACS needed)
# -------------------------
MUSHROOM_VERSION = os.environ.get("MUSHROOM_VERSION", "3.3.0")
MUSHROOM_GITHUB_ZIP = (
    os.environ.get("MUSHROOM_GITHUB_ZIP")
    or f"https://github.com/piitaya/lovelace-mushroom/releases/download/v{MUSHROOM_VERSION}/mushroom.zip"
)
WWW_COMMUNITY = os.path.join(HA_CONFIG_PATH, "www", "community")
MUSHROOM_PATH = os.path.join(WWW_COMMUNITY, "mushroom")

# -------------------------
# Themes
# -------------------------
THEMES_PATH = os.path.join(HA_CONFIG_PATH, "themes")
DASHBOARD_THEME_FILE = os.path.join(THEMES_PATH, "dashboard_maker.yaml")
THEME_NAME = "Dashboard Maker"

# -------------------------
# Helpers
# -------------------------
def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""

def _read_options_json() -> Dict[str, Any]:
    try:
        if os.path.exists(ADDON_OPTIONS_PATH):
            with open(ADDON_OPTIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}

def sanitize_filename(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "_", name)
    if not name:
        name = "unnamed"
    return name[:80]

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
# Token discovery (LLAT + Supervisor)
# -------------------------
def discover_tokens() -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (user_token, supervisor_token)

    Priority for user_token (LLAT):
    1) Add-on options.json: access_token
    2) env HOMEASSISTANT_TOKEN

    Priority for supervisor_token:
    1) Add-on options.json: supervisor_token
    2) env SUPERVISOR_TOKEN
    3) supervisor token files (HAOS)
    """
    opts = _read_options_json()

    # User token (LLAT)
    user_tok = (opts.get("access_token", "") or "").strip()
    if not user_tok:
        user_tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()

    # Supervisor token
    sup_tok = (opts.get("supervisor_token", "") or "").strip()
    if not sup_tok:
        sup_tok = (os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()
    if not sup_tok:
        for p in ("/var/run/supervisor_token", "/run/supervisor_token"):
            sup_tok = _read_file(p)
            if sup_tok:
                break

    return (user_tok or None, sup_tok or None)

USER_TOKEN, SUPERVISOR_TOKEN = discover_tokens()

# Determine connection method
USE_SUPERVISOR_API = bool(SUPERVISOR_TOKEN and not USER_TOKEN)
ACTIVE_TOKEN = SUPERVISOR_TOKEN if USE_SUPERVISOR_API else USER_TOKEN

# Base URLs
HA_SUPERVISOR_URL = os.environ.get("HA_SUPERVISOR_URL", "http://supervisor/core")
HA_DIRECT_URL = os.environ.get("HA_BASE_URL", "http://homeassistant:8123")
HA_BASE_URL = HA_SUPERVISOR_URL if USE_SUPERVISOR_API else HA_DIRECT_URL

# Ensure directories
Path(DASHBOARDS_PATH).mkdir(parents=True, exist_ok=True)
Path(THEMES_PATH).mkdir(parents=True, exist_ok=True)
Path(WWW_COMMUNITY).mkdir(parents=True, exist_ok=True)

print(f"== {APP_NAME} {APP_VERSION} ==")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Dashboards path: {DASHBOARDS_PATH}")
print(f"User token (LLAT) available: {bool(USER_TOKEN)}")
print(f"Supervisor token available: {bool(SUPERVISOR_TOKEN)}")
print(f"Using: {'Supervisor API' if USE_SUPERVISOR_API else 'Direct Core API'}")
print(f"HA base url: {HA_BASE_URL}")
print(f"Active token available: {bool(ACTIVE_TOKEN)}")
print(f"Options JSON found: {os.path.exists(ADDON_OPTIONS_PATH)} at {ADDON_OPTIONS_PATH}")

# -------------------------
# Home Assistant API
# -------------------------
def ha_headers() -> Dict[str, str]:
    if not ACTIVE_TOKEN:
        return {"Content-Type": "application/json"}

    if USE_SUPERVISOR_API:
        return {
            "Authorization": f"Bearer {ACTIVE_TOKEN}",
            "Content-Type": "application/json",
            "X-Supervisor-Token": ACTIVE_TOKEN,
        }
    return {
        "Authorization": f"Bearer {ACTIVE_TOKEN}",
        "Content-Type": "application/json",
    }

def ha_request(method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
    if not path.startswith("/"):
        path = "/" + path

    url = f"{HA_BASE_URL}{path}"
    headers = ha_headers()

    try:
        return requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    except requests.exceptions.RequestException as e:
        # If supervisor API fails, try direct connection as fallback (when we do have supervisor token)
        if USE_SUPERVISOR_API and SUPERVISOR_TOKEN:
            print(f"Supervisor API failed, trying direct: {e}")
            fallback_url = f"{HA_DIRECT_URL}{path}"
            fallback_headers = {
                "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                "Content-Type": "application/json",
            }
            return requests.request(method, fallback_url, headers=fallback_headers, json=json_body, timeout=timeout)
        raise

def ha_ok() -> Tuple[bool, str]:
    if not ACTIVE_TOKEN:
        return False, "Geen token. Maak een Long-Lived Access Token en vul 'access_token' in bij de add-on opties."

    try:
        r = ha_request("GET", "/api/", timeout=10)
        if r.status_code == 200:
            return True, "OK"
        if r.status_code == 401:
            return False, "Token ongeldig (401). Maak een nieuwe Long-Lived Access Token."
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.exceptions.ConnectionError as e:
        return False, f"Kan geen verbinding maken: {str(e)[:120]}"
    except requests.exceptions.Timeout:
        return False, "Timeout: Home Assistant reageert niet"
    except Exception as e:
        return False, f"Fout: {str(e)[:160]}"

def ha_call_service(domain: str, service: str, data: dict | None = None) -> Tuple[Dict[str, Any], int]:
    if not ACTIVE_TOKEN:
        return {"ok": False, "error": "Geen token geconfigureerd."}, 400
    try:
        resp = ha_request("POST", f"/api/services/{domain}/{service}", json_body=(data or {}), timeout=15)
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"Actie mislukt: {resp.status_code}", "details": resp.text[:2000]}, 400
        try:
            return {"ok": True, "result": resp.json()}, 200
        except Exception:
            return {"ok": True, "result": resp.text}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# -------------------------
# Core registry getters
# -------------------------
def get_states() -> List[Dict[str, Any]]:
    ok, _msg = ha_ok()
    if not ok:
        # demo data if no connection
        return [
            {"entity_id": "light.woonkamer", "state": "off", "attributes": {"friendly_name": "Woonkamer Lamp"}},
            {"entity_id": "sensor.temp_woonkamer", "state": "21.1", "attributes": {"friendly_name": "Temperatuur", "unit_of_measurement": "°C", "device_class": "temperature"}},
            {"entity_id": "media_player.tv", "state": "off", "attributes": {"friendly_name": "TV"}},
        ]
    resp = ha_request("GET", "/api/states", timeout=12)
    if resp.status_code != 200:
        print(f"Failed to fetch states: {resp.status_code} - {resp.text[:200]}")
        return []
    return resp.json()

def get_area_registry() -> List[Dict[str, Any]]:
    ok, _msg = ha_ok()
    if not ok:
        return [{"area_id": "woonkamer", "name": "Woonkamer (Beneden)"}, {"area_id": "slaapkamer", "name": "Slaapkamer (Boven)"}]
    resp = ha_request("GET", "/api/config/area_registry", timeout=12)
    if resp.status_code != 200:
        print(f"Failed area_registry: {resp.status_code} - {resp.text[:200]}")
        return []
    return resp.json()

def get_entity_registry() -> List[Dict[str, Any]]:
    ok, _msg = ha_ok()
    if not ok:
        return [{"entity_id": "light.woonkamer", "area_id": "woonkamer"}, {"entity_id": "sensor.temp_woonkamer", "area_id": "woonkamer"}]
    resp = ha_request("GET", "/api/config/entity_registry", timeout=12)
    if resp.status_code != 200:
        print(f"Failed entity_registry: {resp.status_code} - {resp.text[:200]}")
        return []
    return resp.json()

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
        if not entity_id or "." not in entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
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
# Smart filters
# -------------------------
DEFAULT_IGNORE_ENTITY_ID_SUFFIXES = [
    "_rssi", "_linkquality", "_lqi", "_signal_strength", "_signal", "_snr",
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
    eid = e.get("entity_id", "")
    dom = e.get("domain", "")
    name = norm(e.get("name", ""))

    if dom not in DEFAULT_ALLOWED_DOMAINS:
        return True
    if dom in {"automation", "script", "scene", "update"}:
        return True

    if dom == "sensor":
        low = eid.lower()
        if any(low.endswith(suf) for suf in DEFAULT_IGNORE_ENTITY_ID_SUFFIXES):
            return True
        if any(needle in low for needle in DEFAULT_IGNORE_ENTITY_ID_CONTAINS):
            return True
        if any(needle in name for needle in ["rssi", "linkquality", "lqi", "snr", "signal", "uptime", "battery", "diagnostic", "debug"]):
            return True
        if not advanced and not e.get("unit") and not e.get("device_class"):
            return True
    return False

def smart_filter_entities(entities: List[Dict[str, Any]], advanced: bool) -> List[Dict[str, Any]]:
    out = [e for e in entities if not is_ignored_entity(e, advanced=advanced)]
    sensors = [e for e in out if e["domain"] == "sensor"]
    if not advanced and len(sensors) > 24:
        def score(x: Dict[str, Any]) -> int:
            sc = 0
            if x.get("unit"):
                sc += 2
            if x.get("device_class"):
                sc += 2
            if x.get("state_class"):
                sc += 1
            return sc
        sensors_sorted = sorted(sensors, key=score, reverse=True)[:24]
        non = [e for e in out if e["domain"] != "sensor"]
        out = non + sensors_sorted
    return sorted(out, key=lambda x: norm(x.get("name") or x["entity_id"]))

# -------------------------
# Floor detection
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

# -------------------------
# Mushroom / Resources installation
# -------------------------
def mushroom_installed() -> bool:
    return os.path.exists(os.path.join(MUSHROOM_PATH, "mushroom.js"))

def download_and_extract_zip(url: str, target_dir: str):
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        z.extractall(target_dir)

def install_mushroom() -> str:
    os.makedirs(WWW_COMMUNITY, exist_ok=True)
    if mushroom_installed():
        return "Mushroom kaarten zijn al geïnstalleerd"
    download_and_extract_zip(MUSHROOM_GITHUB_ZIP, WWW_COMMUNITY)
    if not mushroom_installed():
        raise RuntimeError("Installeren mislukt (mushroom.js niet gevonden).")
    return "Mushroom kaarten geïnstalleerd"

def get_lovelace_resources() -> List[Dict[str, Any]]:
    try:
        r = ha_request("GET", "/api/lovelace/resources", timeout=12)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def ensure_mushroom_resource() -> str:
    desired_url = "/local/community/mushroom/mushroom.js"
    resources = get_lovelace_resources()
    if any((x.get("url") == desired_url) for x in resources):
        return "Mushroom resource is gekoppeld"
    payload = {"type": "module", "url": desired_url}
    r = ha_request("POST", "/api/lovelace/resources", json_body=payload, timeout=12)
    if r.status_code in (200, 201):
        return "Mushroom resource gekoppeld"
    # best-effort
    return "Mushroom resource (best effort) gekoppeld"

# -------------------------
# Theme presets
# -------------------------
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
    return f"Theme geïnstalleerd: {preset['label']}"

def ha_try_set_theme(theme_name: str, mode: str = "auto") -> Tuple[bool, str]:
    r, st = ha_call_service("frontend", "set_theme", {"name": theme_name, "mode": mode})
    if st == 200 and r.get("ok"):
        return True, "frontend.set_theme (mode)"
    r2, st2 = ha_call_service("frontend", "set_theme", {"name": theme_name})
    if st2 == 200 and r2.get("ok"):
        return True, "frontend.set_theme (fallback)"
    return False, r.get("error") or r2.get("error") or "set_theme failed"

# -------------------------
# Mushroom card helpers
# -------------------------
def _m_title(title: str, subtitle: str = "") -> Dict[str, Any]:
    c = {"type": "custom:mushroom-title-card", "title": title}
    if subtitle:
        c["subtitle"] = subtitle
    return c

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
        return {"type": "custom:mushroom-light-card", "entity": eid, "show_brightness_control": True, "use_light_color": True,
                "tap_action": {"action": "toggle"}, "hold_action": {"action": "more-info"}}
    if domain == "switch":
        return {"type": "custom:mushroom-entity-card", "entity": eid, "tap_action": {"action": "toggle"}, "hold_action": {"action": "more-info"}}
    if domain == "climate":
        return {"type": "custom:mushroom-climate-card", "entity": eid, "show_temperature_control": True, "hvac_modes": True}
    if domain == "media_player":
        return {"type": "custom:mushroom-media-player-card", "entity": eid, "use_media_info": True,
                "show_volume_level": advanced,
                "volume_controls": ["volume_mute", "volume_set"] if advanced else ["volume_mute"],
                "media_controls": ["play_pause", "next", "previous"] if advanced else ["play_pause"]}
    if domain == "cover":
        return {"type": "custom:mushroom-cover-card", "entity": eid, "show_position_control": True, "show_buttons_control": True}
    if domain == "lock":
        return {"type": "custom:mushroom-lock-card", "entity": eid, "tap_action": {"action": "toggle"}}
    if domain == "person":
        return {"type": "custom:mushroom-person-card", "entity": eid, "use_entity_picture": True}
    if domain in {"binary_sensor", "sensor"}:
        return {"type": "custom:mushroom-entity-card", "entity": eid, "tap_action": {"action": "more-info"}}
    return None

# -------------------------
# Grouping / views
# -------------------------
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
        return {"type": "custom:mushroom-template-card", "primary": primary, "secondary": secondary, "icon": icon,
                "tap_action": {"action": "call-service", "service": service, "data": data}}

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

    return [_m_title("Top acties", "1-tik knoppen die iedereen snapt."), _grid(buttons[:6], columns_mobile=2)]

def build_overview_view(all_entities: List[Dict[str, Any]], areas: List[Dict[str, Any]], grouped: Dict[str, List[Dict[str, Any]]], advanced: bool, density: str) -> Dict[str, Any]:
    columns = 2 if density == "comfy" else 3
    chips: List[Dict[str, Any]] = [
        _chip_template("{{ states.light | selectattr('state','eq','on') | list | count }} aan", "mdi:lightbulb-group"),
        _chip_template("{{ now().strftime('%H:%M') }}", "mdi:clock-outline"),
    ]

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

    cards: List[Dict[str, Any]] = [_m_title(area_name, "Alles van deze ruimte, overzichtelijk.")]
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
        views.append(build_area_view(a, ents, advanced=advanced, density=density))

    if include_overig:
        v = build_no_area_view(grouped.get("_no_area_", []), advanced=advanced, density=density)
        if v:
            views.append(v)

    return {"title": dashboard_title, "views": views}

# -------------------------
# Wizard UI (simple but solid)
# -------------------------
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
          <p class="text-gray-600 mt-2">Professionele dashboards zonder HA-kennis. 1x klikken, klaar.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">__APP_VERSION__</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinden…</span>
          </div>
          <div class="flex gap-2 flex-wrap">
            <button onclick="openDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🧾 Debug</button>
          </div>
        </div>
      </div>

      <div id="tokenBox" class="hidden bg-yellow-50 border border-yellow-200 rounded-2xl p-4 mb-6">
        <div class="font-bold text-yellow-800">Token nodig</div>
        <div class="text-sm text-yellow-700 mt-1">
          Maak in Home Assistant een <b>Long-Lived Access Token</b> en plak die in de add-on opties als <code>access_token</code>.
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div class="bg-slate-50 border border-slate-200 rounded-2xl p-4">
          <div class="font-bold text-slate-900">Stap 1 — Auto setup</div>
          <div class="text-sm text-slate-600 mt-1">Installeert Mushroom + koppelt resource + theme (auto licht/donker).</div>
          <div class="mt-3 grid grid-cols-1 gap-2">
            <select id="preset" class="w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-indigo-500 focus:outline-none">
              <option value="indigo_luxe">Indigo Luxe</option>
              <option value="emerald_fresh">Emerald Fresh</option>
              <option value="amber_warm">Amber Warm</option>
              <option value="rose_neon">Rose Neon</option>
            </select>
            <select id="density" class="w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-indigo-500 focus:outline-none">
              <option value="comfy">Comfy (luchtig)</option>
              <option value="compact">Compact (minder scroll)</option>
            </select>
            <button onclick="runSetup()" class="bg-gradient-to-r from-indigo-600 to-purple-600 text-white py-3 px-4 rounded-xl font-semibold hover:from-indigo-700 hover:to-purple-700 shadow-lg">
              🚀 Alles automatisch instellen
            </button>
            <div id="setupOut" class="text-xs text-slate-600 whitespace-pre-line"></div>
          </div>
        </div>

        <div class="bg-slate-50 border border-slate-200 rounded-2xl p-4">
          <div class="font-bold text-slate-900">Stap 2 — Maak dashboards</div>
          <div class="text-sm text-slate-600 mt-1">Wij maken 2 dashboards: <b>Simpel</b> en <b>Uitgebreid</b>.</div>
          <div class="mt-3">
            <input id="dashName" class="w-full px-3 py-3 border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none"
              placeholder="bijv. Thuis" />
            <button onclick="createMine()" class="mt-3 w-full bg-slate-900 text-white py-3 px-4 rounded-xl font-semibold hover:bg-black shadow-lg">
              🎨 Maak mijn dashboards
            </button>
            <div id="createOut" class="text-xs text-slate-600 mt-2 whitespace-pre-line"></div>
          </div>
        </div>
      </div>

      <div class="mt-6 bg-white border border-slate-200 rounded-2xl p-4">
        <div class="flex items-center justify-between">
          <div class="font-bold text-slate-900">Dashboards</div>
          <button onclick="loadDashboards()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔄 Vernieuwen</button>
        </div>
        <div id="dashboardsContent" class="mt-3 space-y-2 text-sm"></div>
      </div>
    </div>
  </div>

<script>
  const API_BASE = window.location.pathname.replace(/\/$/, '');

  function setStatus(text, color='gray') {
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }

  function escapeHtml(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      const cfgRes = await fetch(API_BASE + '/api/config', { cache: 'no-store' });
      const cfg = await cfgRes.json();

      if (!cfg.active_token_configured) {
        document.getElementById('tokenBox').classList.remove('hidden');
        setStatus('Verbonden (token nodig)', 'yellow');
      } else {
        // also test actual HA API reachability
        const okRes = await fetch(API_BASE + '/api/ha_ok', { cache: 'no-store' });
        const ok = await okRes.json();
        if (ok.ok) setStatus('Verbonden', 'green');
        else setStatus('Token/HA fout', 'red');
      }

      await loadDashboards();
    } catch (e) {
      console.error(e);
      setStatus('Verbinding mislukt', 'red');
    }
  }

  async function runSetup() {
    document.getElementById('setupOut').textContent = 'Bezig…';
    const preset = document.getElementById('preset').value;
    const density = document.getElementById('density').value;
    try {
      const res = await fetch(API_BASE + '/api/setup', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ preset, density })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        document.getElementById('setupOut').textContent = '❌ ' + (data.error || 'Onbekend');
        return;
      }
      document.getElementById('setupOut').textContent = '✅ ' + (data.steps || []).join('\\n✅ ');
      alert('✅ Setup klaar!');
      await init();
    } catch (e) {
      console.error(e);
      document.getElementById('setupOut').textContent = '❌ Mislukt.';
    }
  }

  async function createMine() {
    const base_title = document.getElementById('dashName').value.trim();
    if (!base_title) return alert('Vul een naam in');
    document.getElementById('createOut').textContent = 'Bezig…';
    const density = document.getElementById('density').value;

    try {
      const res = await fetch(API_BASE + '/api/create_dashboards', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          base_title,
          include_overview: true,
          include_overig: true,
          include_floor_tabs: true,
          area_ids: null,
          density
        })
      });
      const data = await res.json();
      if (!res.ok || !data.success) {
        document.getElementById('createOut').textContent = '❌ ' + (data.error || 'Onbekend');
        return;
      }
      document.getElementById('createOut').textContent =
        '✅ Gemaakt:\\n- ' + data.simple_filename + '\\n- ' + data.advanced_filename;
      await fetch(API_BASE + '/api/reload_lovelace', { method: 'POST' });
      await loadDashboards();
      alert('✅ Klaar! Dashboards staan in de lijst.');
    } catch (e) {
      console.error(e);
      document.getElementById('createOut').textContent = '❌ Mislukt.';
    }
  }

  async function loadDashboards() {
    const el = document.getElementById('dashboardsContent');
    el.innerHTML = 'Bezig…';
    try {
      const res = await fetch(API_BASE + '/api/dashboards', { cache: 'no-store' });
      const items = await res.json();
      if (!items.length) {
        el.innerHTML = '<div class="text-slate-500">Nog geen dashboards.</div>';
        return;
      }
      el.innerHTML = items.map(t => `
        <div class="bg-slate-50 border border-slate-200 rounded-xl p-3 flex items-center justify-between gap-2">
          <div>
            <div class="font-semibold">${escapeHtml(t.name)}</div>
            <div class="text-xs font-mono text-slate-500">${escapeHtml(t.filename)}</div>
          </div>
          <div class="flex gap-2">
            <button class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100"
              onclick="downloadDashboard('${escapeHtml(t.filename)}')">⬇️</button>
            <button class="text-sm bg-red-500 text-white px-3 py-1 rounded-lg hover:bg-red-600"
              onclick="deleteDashboard('${escapeHtml(t.filename)}')">🗑️</button>
          </div>
        </div>
      `).join('');
    } catch (e) {
      console.error(e);
      el.innerHTML = '<div class="text-red-700">Fout bij laden.</div>';
    }
  }

  async function deleteDashboard(filename) {
    if (!confirm('Verwijderen?')) return;
    const res = await fetch(API_BASE + '/api/delete_dashboard', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ filename })
    });
    const data = await res.json();
    if (!res.ok) return alert('❌ ' + (data.error || 'Onbekend'));
    await loadDashboards();
  }

  function downloadDashboard(filename) {
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

# -------------------------
# API
# -------------------------
@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "user_token_configured": bool(USER_TOKEN),
        "supervisor_token_configured": bool(SUPERVISOR_TOKEN),
        "using_supervisor_api": USE_SUPERVISOR_API,
        "active_token_configured": bool(ACTIVE_TOKEN),
        "dashboards_path": DASHBOARDS_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "mushroom_installed": mushroom_installed(),
        "theme_file_exists": os.path.exists(DASHBOARD_THEME_FILE),
        "ha_base_url": HA_BASE_URL,
        "options_json_found": os.path.exists(ADDON_OPTIONS_PATH),
    })

@app.route("/api/ha_ok", methods=["GET"])
def api_ha_ok():
    ok, msg = ha_ok()
    return jsonify({"ok": ok, "message": msg, "ha_base_url": HA_BASE_URL})

@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    info: Dict[str, Any] = {
        "ha_base_url": HA_BASE_URL,
        "ha_supervisor_url": HA_SUPERVISOR_URL,
        "ha_direct_url": HA_DIRECT_URL,
        "user_token_present": bool(USER_TOKEN),
        "supervisor_token_present": bool(SUPERVISOR_TOKEN),
        "active_token_present": bool(ACTIVE_TOKEN),
        "using_supervisor_api": USE_SUPERVISOR_API,
        "options_json": ADDON_OPTIONS_PATH,
        "options_json_exists": os.path.exists(ADDON_OPTIONS_PATH),
    }

    try:
        if os.path.exists(ADDON_OPTIONS_PATH):
            opts = _read_options_json()
            info["options_keys"] = sorted(list(opts.keys()))
            info["access_token_in_options"] = bool((opts.get("access_token", "") or "").strip())
            info["supervisor_token_in_options"] = bool((opts.get("supervisor_token", "") or "").strip())

        if not ACTIVE_TOKEN:
            return jsonify({
                "ok": False,
                "error": "Geen token gevonden. Maak een Long-Lived Access Token en vul 'access_token' in.",
                "info": info,
            }), 200

        r = ha_request("GET", "/api/", timeout=10)
        return jsonify({
            "ok": (r.status_code == 200),
            "status": r.status_code,
            "body": r.text[:400],
            "info": info,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "info": info}), 200

@app.route("/api/setup", methods=["POST"])
def api_setup():
    if not ACTIVE_TOKEN:
        return jsonify({
            "ok": False,
            "error": "Geen token. Maak een Long-Lived Access Token en voeg toe als 'access_token' in add-on opties."
        }), 400

    data = request.json or {}
    preset = (data.get("preset") or "indigo_luxe").strip()
    density = (data.get("density") or "comfy").strip()

    steps: List[str] = []
    try:
        steps.append(install_mushroom())
        steps.append(ensure_mushroom_resource())
        steps.append(install_dashboard_theme(preset, density))

        ok_theme, _how = ha_try_set_theme(THEME_NAME, mode="auto")
        if ok_theme:
            steps.append("Theme actief (auto licht/donker)")
        else:
            steps.append("Theme geïnstalleerd (activeren niet gelukt, maar vaak OK)")

        # reload lovelace (best effort)
        ha_call_service("lovelace", "reload", {})
        steps.append("Lovelace vernieuwd")

        return jsonify({"ok": True, "steps": steps}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps}), 500

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
        return jsonify({"error": "Naam ontbreekt."}), 400

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
        "simple_code": simple_code,
        "advanced_code": adv_code,
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
    if not ACTIVE_TOKEN:
        return jsonify({"ok": False, "error": "Geen token geconfigureerd."}), 400

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

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"{APP_NAME} starting... ({APP_VERSION})")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8099, debug=False)
