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

APP_VERSION = "2.4.0-fixed-no-globals-homeassistant8123"
APP_NAME = "Dashboard Maker"

app = Flask(__name__)

# -------------------------
# Paths
# -------------------------
HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
DASHBOARDS_PATH = os.environ.get("DASHBOARDS_PATH") or os.path.join(HA_CONFIG_PATH, "dashboards")

ADDON_OPTIONS_PATH = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")

# --- Mushroom install (no HACS needed) ---
MUSHROOM_VERSION = "3.3.0"
MUSHROOM_GITHUB_ZIP = f"https://github.com/piitaya/lovelace-mushroom/releases/download/v{MUSHROOM_VERSION}/mushroom.zip"
WWW_COMMUNITY = os.path.join(HA_CONFIG_PATH, "www", "community")
MUSHROOM_PATH = os.path.join(WWW_COMMUNITY, "mushroom")

# --- Themes ---
THEMES_PATH = os.path.join(HA_CONFIG_PATH, "themes")
DASHBOARD_THEME_FILE = os.path.join(THEMES_PATH, "dashboard_maker.yaml")
THEME_NAME = "Dashboard Maker"

# --- HA URLs ---
HA_SUPERVISOR_URL = os.environ.get("HA_SUPERVISOR_URL", "http://supervisor/core")
HA_DIRECT_URL = os.environ.get("HA_BASE_URL", "http://homeassistant:8123")

Path(DASHBOARDS_PATH).mkdir(parents=True, exist_ok=True)
Path(THEMES_PATH).mkdir(parents=True, exist_ok=True)
Path(WWW_COMMUNITY).mkdir(parents=True, exist_ok=True)

# -------------------------
# Utils
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

# -------------------------
# Token discovery
# -------------------------
def discover_tokens() -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (user_token, supervisor_token)

    user_token = Long-Lived Access Token (LLAT) for direct Core API.
    supervisor_token = Supervisor token for supervisor proxy (rarely manually needed).
    """
    opts = _read_options_json()

    user_tok = (opts.get("access_token", "") or "").strip()
    if not user_tok:
        user_tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()

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

print(f"== {APP_NAME} {APP_VERSION} ==")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Dashboards path: {DASHBOARDS_PATH}")
print(f"Options.json: {ADDON_OPTIONS_PATH} (exists={os.path.exists(ADDON_OPTIONS_PATH)})")
print(f"User token (LLAT) available: {bool(USER_TOKEN)}")
print(f"Supervisor token available: {bool(SUPERVISOR_TOKEN)}")
print(f"HA direct url: {HA_DIRECT_URL}")
print(f"HA supervisor url: {HA_SUPERVISOR_URL}")
print(f"Mushroom path: {MUSHROOM_PATH}")

# -------------------------
# Connection state (NO GLOBALS)
# -------------------------
class HAConnection:
    def __init__(self):
        self.active_base_url: Optional[str] = None
        self.active_token: Optional[str] = None
        self.active_mode: str = "unknown"  # "direct" or "supervisor"
        self.last_probe: Optional[str] = None

    def _headers(self, token: Optional[str], use_supervisor: bool) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
            if use_supervisor:
                # harmless if ignored; helps some proxy stacks
                h["X-Supervisor-Token"] = token
        return h

    def _request_raw(
        self,
        base_url: str,
        token: Optional[str],
        use_supervisor: bool,
        method: str,
        path: str,
        json_body: dict | None,
        timeout: int,
    ) -> requests.Response:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{base_url}{path}"
        return requests.request(method, url, headers=self._headers(token, use_supervisor), json=json_body, timeout=timeout)

    def probe(self, force: bool = False) -> Tuple[bool, str]:
        """
        Decide best working connection and cache it.
        Order:
          1) If USER_TOKEN exists: try direct
          2) If SUPERVISOR_TOKEN exists: try supervisor proxy
          3) Fallback: try the other if available
        """
        if self.active_base_url and self.active_token and not force:
            return True, f"cached:{self.active_mode}"

        # refresh tokens each probe (options can change without restart sometimes)
        global USER_TOKEN, SUPERVISOR_TOKEN
        USER_TOKEN, SUPERVISOR_TOKEN = discover_tokens()

        attempts: List[Tuple[str, str, Optional[str], bool]] = []

        if USER_TOKEN:
            attempts.append(("direct", HA_DIRECT_URL, USER_TOKEN, False))
        if SUPERVISOR_TOKEN:
            attempts.append(("supervisor", HA_SUPERVISOR_URL, SUPERVISOR_TOKEN, True))

        # fallback attempts (reverse)
        if USER_TOKEN and SUPERVISOR_TOKEN:
            # already both in list; no extra needed
            pass

        if not attempts:
            self.active_base_url = None
            self.active_token = None
            self.active_mode = "unknown"
            self.last_probe = "no_token"
            return False, "Geen token. Voeg 'access_token' toe (LLAT) in add-on opties."

        last_err = ""
        for mode, base, tok, use_sup in attempts:
            try:
                r = self._request_raw(base, tok, use_sup, "GET", "/api/", None, 8)
                if r.status_code == 200:
                    self.active_base_url = base
                    self.active_token = tok
                    self.active_mode = mode
                    self.last_probe = "ok"
                    return True, f"OK via {mode} ({base})"
                if r.status_code in (401, 403):
                    last_err = f"{mode}: unauthorized ({r.status_code})"
                else:
                    last_err = f"{mode}: http {r.status_code}: {r.text[:120]}"
            except requests.exceptions.RequestException as e:
                last_err = f"{mode}: connection error: {str(e)[:120]}"

        self.active_base_url = None
        self.active_token = None
        self.active_mode = "unknown"
        self.last_probe = last_err or "probe_failed"
        return False, last_err or "Probe failed"

    def request(self, method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
        ok, _ = self.probe(force=False)
        if not ok or not self.active_base_url:
            # force re-probe once
            ok2, msg2 = self.probe(force=True)
            if not ok2 or not self.active_base_url:
                raise requests.exceptions.ConnectionError(msg2)

        use_sup = (self.active_mode == "supervisor")
        r = self._request_raw(self.active_base_url, self.active_token, use_sup, method, path, json_body, timeout)

        # if token expired/incorrect, retry once after reprobe
        if r.status_code in (401, 403):
            self.active_base_url = None
            self.active_token = None
            self.active_mode = "unknown"
            ok3, _ = self.probe(force=True)
            if ok3 and self.active_base_url:
                use_sup = (self.active_mode == "supervisor")
                r = self._request_raw(self.active_base_url, self.active_token, use_sup, method, path, json_body, timeout)

        return r

conn = HAConnection()

def ha_call_service(domain: str, service: str, data: dict | None = None) -> Tuple[Dict[str, Any], int]:
    try:
        resp = conn.request("POST", f"/api/services/{domain}/{service}", json_body=(data or {}), timeout=15)
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"{domain}.{service} failed: {resp.status_code}", "details": resp.text[:2000]}, 400
        try:
            return {"ok": True, "result": resp.json()}, 200
        except Exception:
            return {"ok": True, "result": resp.text}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# -------------------------
# HA data helpers
# -------------------------
def get_states() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/states", timeout=12)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []

def get_area_registry() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/config/area_registry", timeout=12)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []

def get_entity_registry() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/config/entity_registry", timeout=12)
        if r.status_code != 200:
            return []
        return r.json()
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

# -------------------------
# Smart filters (anti-clutter)
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
        for suf in DEFAULT_IGNORE_ENTITY_ID_SUFFIXES:
            if low.endswith(suf):
                return True
        for needle in DEFAULT_IGNORE_ENTITY_ID_CONTAINS:
            if needle in low:
                return True
        for needle in ["rssi", "linkquality", "lqi", "snr", "signal", "uptime", "battery", "diagnostic", "debug"]:
            if needle in name:
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
# Mushroom install + resource
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
        raise RuntimeError("Mushroom install faalde: mushroom.js niet gevonden.")
    return "Mushroom kaarten geïnstalleerd"

def get_lovelace_resources() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/lovelace/resources", timeout=12)
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
        return "Mushroom resource staat goed"
    payload = {"type": "module", "url": desired_url}
    try:
        r = conn.request("POST", "/api/lovelace/resources", json_body=payload, timeout=12)
        if r.status_code in (200, 201):
            return "Mushroom resource toegevoegd"
        # best effort: HA kan 400 geven als al bestaat
        return "Mushroom resource (best effort) OK"
    except Exception:
        return "Mushroom resource (best effort) OK"

# -------------------------
# Theme generator (auto light/dark)
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
  disabled-text-color: "rgba(15, 23, 42, 0.42)"
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
    return f"Theme geschreven: {preset['label']}"

def try_set_theme_auto() -> str:
    # Try modern (mode)
    r, st = ha_call_service("frontend", "set_theme", {"name": THEME_NAME, "mode": "auto"})
    if st == 200 and r.get("ok"):
        return "Theme actief (auto licht/donker)"
    # Fallback
    r2, st2 = ha_call_service("frontend", "set_theme", {"name": THEME_NAME})
    if st2 == 200 and r2.get("ok"):
        return "Theme actief (fallback)"
    return "Theme geïnstalleerd (activeren niet gelukt, maar vaak wel beschikbaar)"

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
        _m_title("Top acties", "1-tik knoppen die iedereen snapt."),
        _grid(buttons[:6], columns_mobile=2),
    ]

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

# -------------------------
# Wizard UI
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
          <p class="text-gray-600 mt-2">Klik, kies stijl, klaar. Professionele dashboards — zonder technische kennis.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">__APP_VERSION__</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinden…</span>
          </div>
          <div class="flex gap-2 flex-wrap">
            <button onclick="openDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🧾 Debug
            </button>
          </div>
        </div>
      </div>

      <div class="bg-slate-50 border border-slate-200 rounded-2xl p-4 mb-6">
        <div class="text-sm text-slate-700">
          <b>Tip:</b> Heb je een “Service Token / Long-Lived Access Token”? Zet die in add-on opties als <code>access_token</code>.
        </div>
      </div>

      <div class="border border-slate-200 rounded-2xl p-5">
        <h2 class="text-xl font-bold text-slate-900">Stap 1 — Super setup (automatisch)</h2>
        <p class="text-slate-600 mt-1">Installeert Mushroom + theme (auto licht/donker).</p>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Stijl</div>
            <select id="preset" class="mt-2 w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-indigo-500 focus:outline-none">
              <option value="indigo_luxe">Indigo Luxe</option>
              <option value="emerald_fresh">Emerald Fresh</option>
              <option value="amber_warm">Amber Warm</option>
              <option value="rose_neon">Rose Neon</option>
            </select>
          </div>
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Layout</div>
            <select id="density" class="mt-2 w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-indigo-500 focus:outline-none">
              <option value="comfy">Comfy (luchtig)</option>
              <option value="compact">Compact (minder scroll)</option>
            </select>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
          <div class="bg-white border border-slate-200 rounded-xl p-4">
            <div class="font-semibold">Verbinding</div>
            <div id="chkConn" class="text-sm mt-1 text-slate-500">⏳ wachten…</div>
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
          <button onclick="createDemo()" class="w-full sm:w-auto bg-slate-900 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-black shadow-lg">
            ✨ Maak WOW demo dashboard
          </button>
        </div>

        <div class="mt-5 border-t pt-5">
          <h3 class="text-lg font-bold text-slate-900">Maak jouw dashboards</h3>
          <p class="text-slate-600 mt-1">Wij maken automatisch 2 dashboards: <b>Simpel</b> & <b>Uitgebreid</b>.</p>

          <div class="mt-3">
            <label class="block text-base font-semibold text-gray-700 mb-2">Naam</label>
            <input type="text" id="dashName" placeholder="bijv. Thuis"
                   class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none">
          </div>

          <div class="mt-3 flex flex-col sm:flex-row gap-3">
            <button onclick="createMine()" class="w-full sm:w-auto bg-gradient-to-r from-indigo-600 to-purple-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-indigo-700 hover:to-purple-700 shadow-lg">
              🎨 Maak mijn dashboards
            </button>
            <button onclick="loadDashboards()" class="w-full sm:w-auto bg-white border border-gray-300 text-gray-800 py-3 px-4 rounded-xl text-lg font-semibold hover:bg-gray-100 shadow-lg">
              📋 Toon dashboards
            </button>
          </div>
        </div>
      </div>
    </div>

    <div id="dashboardsList" class="bg-white rounded-2xl shadow-2xl p-6 sm:p-8 hidden">
      <h2 class="text-2xl font-bold text-gray-800 mb-4">📚 Dashboards</h2>
      <div id="dashboardsContent" class="space-y-3"></div>
    </div>
  </div>

<script>
  function setStatus(text, color='gray') {
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }
  function setCheck(id, ok, msg) {
    const el = document.getElementById(id);
    el.textContent = (ok ? '✅ ' : '❌ ') + msg;
    el.className = 'text-sm mt-1 ' + (ok ? 'text-green-700' : 'text-red-700');
  }
  function escapeHtml(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      const r = await fetch('/api/config', {cache:'no-store'});
      const cfg = await r.json();

      if (cfg.ha_ok) {
        setStatus('Verbonden (' + cfg.ha_mode + ')', 'green');
        setCheck('chkConn', true, 'OK via ' + cfg.ha_mode);
      } else {
        setStatus('Niet verbonden', 'red');
        setCheck('chkConn', false, cfg.ha_error || 'Geen verbinding');
      }

      setCheck('chkCards', cfg.mushroom_installed, cfg.mushroom_installed ? 'Geïnstalleerd' : 'Niet geïnstalleerd');
      setCheck('chkStyle', cfg.theme_file_exists, cfg.theme_file_exists ? 'Beschikbaar' : 'Niet geïnstalleerd');
    } catch(e) {
      console.error(e);
      setStatus('Verbinding mislukt', 'red');
      setCheck('chkConn', false, 'Fetch /api/config faalde');
      setCheck('chkCards', false, 'Onbekend');
      setCheck('chkStyle', false, 'Onbekend');
    }
  }

  async function runSetup() {
    const preset = document.getElementById('preset').value;
    const density = document.getElementById('density').value;

    try {
      const res = await fetch('/api/setup', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({preset, density})
      });
      const data = await res.json();
      if (!res.ok || !data.ok) return alert('❌ Setup mislukt: ' + (data.error || 'Onbekend'));

      alert('✅ Setup klaar!\n\n' + (data.steps || []).join('\n'));
      init();
    } catch(e) {
      console.error(e);
      alert('❌ Setup mislukt.');
    }
  }

  async function createDemo() {
    const density = document.getElementById('density').value;
    try {
      const res = await fetch('/api/create_demo', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({density})
      });
      const data = await res.json();
      if (!res.ok || !data.success) return alert('❌ Demo mislukt: ' + (data.error || 'Onbekend'));
      alert('✅ Demo gemaakt: ' + data.filename);
      loadDashboards();
    } catch(e) {
      console.error(e);
      alert('❌ Demo mislukt.');
    }
  }

  async function createMine() {
    const base_title = document.getElementById('dashName').value.trim();
    if (!base_title) return alert('❌ Vul een naam in.');
    const density = document.getElementById('density').value;

    try {
      const res = await fetch('/api/create_dashboards', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          base_title,
          include_overview:true,
          include_overig:true,
          include_floor_tabs:true,
          area_ids:null,
          density
        })
      });
      const data = await res.json();
      if (!res.ok || !data.success) return alert('❌ Maken mislukt: ' + (data.error || 'Onbekend'));

      alert('✅ Klaar!\n- ' + data.simple_filename + '\n- ' + data.advanced_filename);
      loadDashboards();
    } catch(e) {
      console.error(e);
      alert('❌ Maken mislukt.');
    }
  }

  async function loadDashboards() {
    const response = await fetch('/api/dashboards');
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
    const response = await fetch('/api/delete_dashboard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename })
    });
    const result = await response.json();
    if (response.ok) {
      alert('✅ Verwijderd!');
      loadDashboards();
    } else {
      alert('❌ Fout: ' + (result.error || 'Onbekend'));
    }
  }

  async function downloadDashboard(filename) {
    window.open('/api/download?filename=' + encodeURIComponent(filename), '_blank');
  }

  async function openDebug() {
    const res = await fetch('/api/debug/ha', {cache:'no-store'});
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
# API routes
# -------------------------
@app.route("/api/config", methods=["GET"])
def api_config():
    ok, msg = conn.probe(force=False)
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,

        "user_token_configured": bool(USER_TOKEN),
        "supervisor_token_configured": bool(SUPERVISOR_TOKEN),
        "active_mode": conn.active_mode,
        "active_base_url": conn.active_base_url,
        "options_json_found": os.path.exists(ADDON_OPTIONS_PATH),

        "ha_ok": ok,
        "ha_mode": conn.active_mode if ok else "none",
        "ha_error": None if ok else msg,

        "dashboards_path": DASHBOARDS_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "mushroom_installed": mushroom_installed(),
        "theme_file_exists": os.path.exists(DASHBOARD_THEME_FILE),
    })

@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    info: Dict[str, Any] = {
        "ha_direct_url": HA_DIRECT_URL,
        "ha_supervisor_url": HA_SUPERVISOR_URL,
        "options_json": ADDON_OPTIONS_PATH,
        "options_json_exists": os.path.exists(ADDON_OPTIONS_PATH),
        "user_token_present": bool(USER_TOKEN),
        "supervisor_token_present": bool(SUPERVISOR_TOKEN),
        "active_mode": conn.active_mode,
        "active_base_url": conn.active_base_url,
        "last_probe": conn.last_probe,
    }
    if os.path.exists(ADDON_OPTIONS_PATH):
        opts = _read_options_json()
        info["options_keys"] = sorted(list(opts.keys()))
        info["access_token_in_options"] = bool((opts.get("access_token", "") or "").strip())
        info["supervisor_token_in_options"] = bool((opts.get("supervisor_token", "") or "").strip())

    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"ok": False, "error": msg, "info": info}), 200

    try:
        r = conn.request("GET", "/api/", timeout=10)
        return jsonify({"ok": (r.status_code == 200), "status": r.status_code, "body": r.text[:400], "info": info}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "info": info}), 200

@app.route("/api/setup", methods=["POST"])
def api_setup():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({
            "ok": False,
            "error": "Geen verbinding met Home Assistant. Zet je Long-Lived Access Token in add-on opties als 'access_token'. "
                     f"Details: {msg}"
        }), 400

    data = request.json or {}
    preset = (data.get("preset") or "indigo_luxe").strip()
    density = (data.get("density") or "comfy").strip()

    steps: List[str] = []
    try:
        steps.append(install_mushroom())
        steps.append(ensure_mushroom_resource())
        steps.append(install_dashboard_theme(preset, density))
        steps.append(try_set_theme_auto())

        # reload lovelace best-effort
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
    base_title = (data.get("base_title") or "").strip()
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
