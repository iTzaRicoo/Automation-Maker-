from __future__ import annotations

from flask import Flask, request, jsonify, Response
import os
import re
import shutil
from pathlib import Path
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import yaml
import zipfile
import io
import json  # ✅ Ensure json is imported
import time
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -----------------------------------------------------------------------------
# App metadata
# -----------------------------------------------------------------------------
APP_NAME = os.environ.get("APP_NAME", "Dashboard Maker")
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Paths / constants
# -----------------------------------------------------------------------------
CONFIGURED_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
HA_CONFIG_PATH = os.path.abspath(CONFIGURED_CONFIG_PATH)

DASHBOARDS_PATH = os.path.join(HA_CONFIG_PATH, "dashboards")
WWW_PATH = os.path.join(HA_CONFIG_PATH, "www")
COMMUNITY_PATH = os.path.join(WWW_PATH, "community")
MUSHROOM_PATH = os.path.join(COMMUNITY_PATH, "lovelace-mushroom")

THEMES_PATH = os.path.join(HA_CONFIG_PATH, "themes")
DASHBOARD_THEME_DIR = os.path.join(THEMES_PATH, "dashboard_maker")
DASHBOARD_THEME_FILE = os.path.join(DASHBOARD_THEME_DIR, "dashboard_maker.yaml")

ADDON_OPTIONS_PATH = "/data/options.json"  # HA add-on options
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"
HOMEASSISTANT_TOKEN_ENV = "HOMEASSISTANT_TOKEN"

# Prefer supervisor when available; fall back to local HA.
HA_URLS = [
    "http://supervisor/core",
    "http://homeassistant:8123",
    "http://127.0.0.1:8123",
]

DEFAULT_MUSHROOM_ZIP = "https://github.com/piitaya/lovelace-mushroom/releases/latest/download/lovelace-mushroom.zip"


# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitize_filename(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "_", s)
    s = re.sub(r"^_+|_+$", "", s)
    return s or "dashboard"


def next_available_filename(folder: str, filename: str) -> str:
    ensure_dir(folder)
    base = Path(filename).stem
    ext = Path(filename).suffix or ".yaml"
    candidate = f"{base}{ext}"
    i = 1
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{base}_{i}{ext}"
        i += 1
    return candidate


def write_text_file(path: str, content: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def list_yaml_files(folder: str) -> List[str]:
    if not os.path.exists(folder):
        return []
    files: List[str] = []
    for fn in os.listdir(folder):
        if fn.lower().endswith((".yaml", ".yml")):
            files.append(fn)
    return sorted(files)


def safe_yaml_dump(data: Any) -> str:
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _read_options_json() -> Dict[str, Any]:
    if not os.path.exists(ADDON_OPTIONS_PATH):
        return {}
    try:
        with open(ADDON_OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"options.json read error: {e}")
        return {}


# -----------------------------------------------------------------------------
# Home Assistant Connection
# -----------------------------------------------------------------------------
class HAConnection:
    def __init__(self) -> None:
        self.user_token: Optional[str] = None
        self.supervisor_token: Optional[str] = None

        self.active_base_url: Optional[str] = None
        self.active_token: Optional[str] = None
        self.active_mode: str = "unknown"

        self.last_probe: str = ""
        self.probe_attempts: List[Dict[str, Any]] = []
        self.token_debug: Dict[str, Any] = {}

        self.refresh_tokens()

    def refresh_tokens(self) -> None:
        opts = _read_options_json()
        access_token = (opts.get("access_token") or "").strip()
        self.user_token = access_token or os.environ.get(HOMEASSISTANT_TOKEN_ENV)
        self.supervisor_token = os.environ.get(SUPERVISOR_TOKEN_ENV)

        self.token_debug = {
            "options_json_exists": os.path.exists(ADDON_OPTIONS_PATH),
            "options_json_path": ADDON_OPTIONS_PATH,
            "access_token_in_options": bool(access_token),
            "HOMEASSISTANT_TOKEN_env": bool(os.environ.get(HOMEASSISTANT_TOKEN_ENV)),
            "SUPERVISOR_TOKEN_env": bool(os.environ.get(SUPERVISOR_TOKEN_ENV)),
            "user_token_length": len(self.user_token) if self.user_token else 0,
            "supervisor_token_length": len(self.supervisor_token) if self.supervisor_token else 0,
            "ha_urls": HA_URLS,
        }

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}" if token else "",
            "Content-Type": "application/json",
        }

    # ✅ Fix 1: Verbeterde _test_connection met JSON validation
    def _test_connection(self, url: str, token: Optional[str], mode: str) -> Tuple[bool, str, Dict[str, Any]]:
        debug = {
            "url": url,
            "mode": mode,
            "token_provided": bool(token),
            "token_length": len(token) if token else 0,
        }
        if not token:
            return False, "No token", debug

        try:
            test_url = f"{url}/api/"
            debug["test_url"] = test_url

            r = requests.get(
                test_url,
                headers=self._headers(token),
                timeout=10,
                verify=False
            )

            debug["status_code"] = r.status_code
            debug["response_length"] = len(r.text)
            debug["content_type"] = r.headers.get("Content-Type", "")

            if r.status_code == 200:
                try:
                    # ✅ Check of response JSON is
                    content_type = r.headers.get("Content-Type", "")
                    if "application/json" not in content_type:
                        debug["error"] = f"Response is geen JSON (Content-Type: {content_type})"
                        debug["response_text"] = r.text[:500]
                        return False, "Geen JSON response", debug

                    # ✅ Probeer JSON te parsen
                    data = r.json()
                    debug["response_message"] = (data or {}).get("message", "")
                    debug["response_data"] = str(data)[:200]
                    return True, "OK", debug

                except json.JSONDecodeError as e:
                    debug["json_error"] = str(e)
                    debug["response_text"] = r.text[:500]
                    debug["error"] = f"JSON parse error: {str(e)}"

                    # ✅ Check of het HTML is (login page)
                    if r.text.strip().startswith("<"):
                        debug["error"] = "Response is HTML (mogelijk login page)"
                        return False, "HTML response (login page?)", debug

                    return False, f"Ongeldige JSON: {str(e)[:50]}", debug

            # Auth errors
            if r.status_code == 401:
                debug["error"] = "Unauthorized - token werkt niet"
                debug["response_text"] = r.text[:300]
                return False, "Token ongeldig (401)", debug

            if r.status_code == 403:
                debug["error"] = "Forbidden - geen toegang"
                debug["response_text"] = r.text[:300]
                return False, "Geen toegang (403)", debug

            # Andere HTTP errors
            debug["error"] = f"HTTP {r.status_code}"
            debug["response_text"] = r.text[:300]
            return False, f"HTTP {r.status_code}", debug

        except requests.exceptions.Timeout:
            debug["error"] = "Timeout na 10 seconden"
            return False, "Timeout", debug
        except requests.exceptions.ConnectionError as e:
            debug["error"] = f"Connection error: {str(e)[:200]}"
            return False, "Connection refused", debug
        except Exception as e:
            debug["error"] = f"Exception: {str(e)[:200]}"
            return False, str(e)[:100], debug

    # ✅ Fix 2: Verbeterde probe met betere error reporting
    def probe(self, force: bool = False) -> Tuple[bool, str]:
        if self.active_base_url and self.active_token and not force:
            return True, f"cached:{self.active_mode}"

        self.refresh_tokens()
        self.probe_attempts = []

        attempts: List[Tuple[str, str, str]] = []

        # Probeer user token eerst
        if self.user_token:
            for url in HA_URLS:
                mode = "user_token"
                attempts.append((url, self.user_token, mode))

        # Probeer supervisor token
        if self.supervisor_token:
            for url in HA_URLS:
                if "supervisor" in url:
                    attempts.append((url, self.supervisor_token, "supervisor"))

        if not attempts:
            msg = (
                "❌ Geen tokens gevonden!\n\n"
                "Oplossing:\n"
                "1. Ga naar je Home Assistant profiel\n"
                "2. Scroll naar 'Long-Lived Access Tokens'\n"
                "3. Klik 'Create Token'\n"
                "4. Kopieer de token\n"
                "5. Plak in add-on configuratie als 'access_token'\n"
            )
            self.last_probe = msg
            return False, msg

        print(f"\n🔍 Testing {len(attempts)} connection attempts...")

        all_errors: List[str] = []

        for url, token, mode in attempts:
            success, message, debug = self._test_connection(url, token, mode)
            self.probe_attempts.append(debug)
            print(f"  {'✓' if success else '✗'} {mode:15} {url:35} → {message}")

            if not success:
                error_detail = debug.get("error", message)
                all_errors.append(f"{mode} @ {url}: {error_detail}")

            if success:
                self.active_base_url = url
                self.active_token = token
                self.active_mode = mode
                self.last_probe = "ok"
                print(f"  ✅ Connected via: {mode} at {url}\n")
                return True, f"OK via {mode}"

        # Alle pogingen gefaald
        error_msg = "❌ Alle verbindingen gefaald!\n\n"
        error_msg += "Geprobeerde verbindingen:\n"
        for err in all_errors[:5]:
            error_msg += f"  • {err}\n"

        error_msg += "\n💡 Meest voorkomende oorzaken:\n"
        error_msg += "1. Token is niet correct of verlopen\n"
        error_msg += "2. Home Assistant geeft HTML ipv JSON (login page?)\n"
        error_msg += "3. Verkeerde URL/endpoint\n"
        error_msg += "4. Firewall/netwerk blokkade\n\n"

        error_msg += "🔧 Oplossing:\n"
        error_msg += "1. Maak NIEUWE Long-Lived Access Token in HA\n"
        error_msg += "2. Zet token in add-on config: access_token\n"
        error_msg += "3. Herstart add-on\n"
        error_msg += "4. Check logs voor details\n"

        self.last_probe = error_msg
        print(error_msg)
        return False, error_msg

    # ✅ Fix 3: Verbeterde request methode met response validation
    def request(self, method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
        ok, _ = self.probe(force=False)
        if not ok or not self.active_base_url:
            ok2, msg2 = self.probe(force=True)
            if not ok2 or not self.active_base_url:
                raise requests.exceptions.ConnectionError(msg2)

        if not path.startswith("/"):
            path = "/" + path

        url = f"{self.active_base_url}{path}"

        try:
            r = requests.request(
                method,
                url,
                headers=self._headers(self.active_token),
                json=json_body,
                timeout=timeout,
                verify=False
            )

            # ✅ Log response details
            content_type = r.headers.get("Content-Type", "unknown")
            print(f"📡 {method} {path} → {r.status_code} ({content_type})")

            # ✅ Check voor HTML response (betekent meestal auth probleem)
            if r.status_code == 200 and "text/html" in content_type:
                print("⚠️ WARNING: Got HTML response instead of JSON")
                print(f"   Response preview: {r.text[:200]}")

            # Auth errors - re-probe
            if r.status_code in (401, 403):
                print("⚠️ Auth error, re-probing...")
                self.active_base_url = None
                self.active_token = None
                self.active_mode = "unknown"
                ok3, _ = self.probe(force=True)
                if ok3 and self.active_base_url:
                    url = f"{self.active_base_url}{path}"
                    r = requests.request(
                        method,
                        url,
                        headers=self._headers(self.active_token),
                        json=json_body,
                        timeout=timeout,
                        verify=False
                    )

            return r

        except requests.exceptions.RequestException as e:
            print(f"❌ Request failed: {method} {path} - {str(e)}")
            raise


conn = HAConnection()

# -----------------------------------------------------------------------------
# HA helpers
# -----------------------------------------------------------------------------
def safe_get_states() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/states", timeout=25)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"safe_get_states error: {e}")
    return []


def get_area_registry() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/config/area_registry", timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"get_area_registry error: {e}")
    return []


def get_entity_registry() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/config/entity_registry", timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"get_entity_registry error: {e}")
    return []


def ha_call_service(domain: str, service: str, data: Dict[str, Any]) -> Dict[str, Any]:
    payload = data or {}
    r = conn.request("POST", f"/api/services/{domain}/{service}", json_body=payload, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Service call failed: {domain}.{service} HTTP {r.status_code} - {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return {"ok": True}


def get_lovelace_resources() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/lovelace/resources", timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"get_lovelace_resources error: {e}")
    return []


# -----------------------------------------------------------------------------
# Mushroom install / resource
# -----------------------------------------------------------------------------
def mushroom_installed() -> bool:
    possible_paths = [
        os.path.join(MUSHROOM_PATH, "dist"),
        os.path.join(MUSHROOM_PATH, "build"),
        MUSHROOM_PATH
    ]

    for check_path in possible_paths:
        if os.path.exists(check_path):
            try:
                for root, _dirs, files in os.walk(check_path):
                    for f in files:
                        if f.endswith(".js"):
                            return True
            except Exception as e:
                print(f"Check error in {check_path}: {e}")

    return False


def download_and_extract_zip(url: str, target_dir: str) -> None:
    print(f"Downloading Mushroom from: {url}")
    r = requests.get(url, timeout=45, verify=False)
    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        temp_extract = os.path.join(target_dir, "_temp_extract")
        os.makedirs(temp_extract, exist_ok=True)
        z.extractall(temp_extract)

    extracted_items = os.listdir(temp_extract)
    if not extracted_items:
        shutil.rmtree(temp_extract)
        raise RuntimeError("Zip was leeg")

    source_folder = os.path.join(temp_extract, extracted_items[0])
    final_path = os.path.join(target_dir, "lovelace-mushroom")

    if os.path.exists(final_path):
        shutil.rmtree(final_path)

    shutil.move(source_folder, final_path)
    shutil.rmtree(temp_extract)

    print(f"Mushroom geïnstalleerd in: {final_path}")


def install_mushroom() -> str:
    ensure_dir(COMMUNITY_PATH)
    if mushroom_installed():
        return "✅ Mushroom al geïnstalleerd"

    opts = _read_options_json()
    zip_url = (opts.get("mushroom_zip_url") or DEFAULT_MUSHROOM_ZIP).strip()

    download_and_extract_zip(zip_url, COMMUNITY_PATH)
    return "✅ Mushroom geïnstalleerd"


def ensure_mushroom_resource() -> str:
    local_url = "/local/community/lovelace-mushroom/dist/mushroom.js"
    cdn_url = "https://unpkg.com/lovelace-mushroom@latest/dist/mushroom.js"

    resources = get_lovelace_resources()

    for res in resources:
        url = res.get("url", "")
        if local_url in url or "mushroom" in url:
            return "✅ Mushroom resource staat goed"

    for url_to_try in [local_url, cdn_url]:
        payload = {"type": "module", "url": url_to_try}
        try:
            r = conn.request("POST", "/api/lovelace/resources", json_body=payload, timeout=12)
            if r.status_code in (200, 201):
                source = "lokaal" if "local" in url_to_try else "CDN"
                return f"✅ Mushroom resource toegevoegd ({source})"
        except Exception as e:
            print(f"Resource registration via {url_to_try} failed: {e}")
            continue

    return "✅ Mushroom resource (best effort) OK"


# -----------------------------------------------------------------------------
# Theme
# -----------------------------------------------------------------------------
def install_dashboard_theme(preset: str, density: str) -> str:
    ensure_dir(DASHBOARD_THEME_DIR)
    content = f"""# Dashboard Maker Theme
dashboard_maker:
  preset: "{preset}"
  dashboard_density: "{density}"
"""
    write_text_file(DASHBOARD_THEME_FILE, content)
    return "✅ Theme geïnstalleerd"


def try_set_theme_auto() -> str:
    try:
        ha_call_service("frontend", "set_theme", {"name": "dashboard_maker", "mode": "auto"})
        return "✅ Theme geactiveerd (auto)"
    except Exception as e:
        print(f"try_set_theme_auto warning: {e}")
        return "⚠️ Theme activeren niet gelukt (best effort)"


# -----------------------------------------------------------------------------
# configuration.yaml lovelace helpers
# -----------------------------------------------------------------------------
def backup_configuration_yaml() -> Optional[str]:
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")
    if not os.path.exists(config_yaml_path):
        return None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(HA_CONFIG_PATH, f"configuration.yaml.backup_{timestamp}")
        shutil.copy2(config_yaml_path, backup_path)
        print(f"💾 Backup gemaakt: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"⚠️ Backup gefaald: {e}")
        return None


def ensure_lovelace_config() -> Tuple[bool, str]:
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")
    backup_path = None

    if os.path.exists(config_yaml_path):
        try:
            with open(config_yaml_path, "r", encoding="utf-8") as f:
                content = f.read()
                config = yaml.safe_load(content) or {}
        except Exception as e:
            return False, f"Kan configuration.yaml niet lezen: {e}"
    else:
        config = {}

    if not isinstance(config, dict):
        config = {}

    lovelace = config.get("lovelace")
    needs_update = False

    if not isinstance(lovelace, dict):
        if not backup_path and os.path.exists(config_yaml_path):
            backup_path = backup_configuration_yaml()
        lovelace = {"mode": "yaml", "dashboards": {}}
        config["lovelace"] = lovelace
        needs_update = True
    else:
        if lovelace.get("mode") != "yaml":
            if not backup_path and os.path.exists(config_yaml_path):
                backup_path = backup_configuration_yaml()
            lovelace["mode"] = "yaml"
            needs_update = True
        if not isinstance(lovelace.get("dashboards"), dict):
            lovelace["dashboards"] = {}
            needs_update = True

    if needs_update:
        try:
            with open(config_yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            msg = "✅ configuration.yaml bijgewerkt"
            if backup_path:
                msg += f" (backup: {os.path.basename(backup_path)})"
            return True, msg
        except Exception as e:
            return False, f"Kan configuration.yaml niet schrijven: {e}"

    return True, "Lovelace config al correct"


def register_dashboard_in_lovelace(filename: str, title: str, editable: bool = False) -> str:
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    ok, msg = ensure_lovelace_config()
    if not ok:
        return f"Config setup gefaald: {msg}"

    try:
        with open(config_yaml_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        return f"Kan configuration.yaml niet lezen: {e}"

    if not isinstance(config, dict):
        config = {}

    lovelace = config.get("lovelace", {}) if isinstance(config.get("lovelace"), dict) else {"mode": "yaml", "dashboards": {}}
    dashboards = lovelace.get("dashboards", {}) if isinstance(lovelace.get("dashboards"), dict) else {}

    base_key = filename.replace(".yaml", "").replace("_", "-").replace(" ", "-").lower()
    base_key = re.sub(r"-?\d+$", "", base_key)
    if not base_key or base_key in ["dashboard", "dashboards"]:
        base_key = "dash"

    dashboard_key = base_key
    counter = 1
    while dashboard_key in dashboards:
        dashboard_key = f"{base_key}-{counter}"
        counter += 1

    dashboards[dashboard_key] = {
        "mode": "yaml",
        "title": title,
        "icon": "mdi:view-dashboard",
        "show_in_sidebar": True,
        "filename": f"dashboards/{filename}",
    }

    lovelace["dashboards"] = dashboards
    config["lovelace"] = lovelace

    try:
        with open(config_yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return f"Dashboard '{title}' geregistreerd als '{dashboard_key}'"
    except Exception as e:
        return f"Schrijven gefaald: {e}"


# -----------------------------------------------------------------------------
# Dashboard builders
# -----------------------------------------------------------------------------
def build_simple_single_page_dashboard(title: str) -> Dict[str, Any]:
    states = safe_get_states()

    cards: List[Dict[str, Any]] = [{
        "type": "custom:mushroom-title-card",
        "title": title,
        "subtitle": "{{ now().strftime('%d %B %Y') }}"
    }]

    lights = [e for e in states if (e.get("entity_id", "") or "").startswith("light.")][:8]
    switches = [e for e in states if (e.get("entity_id", "") or "").startswith("switch.")][:6]
    climate = [e for e in states if (e.get("entity_id", "") or "").startswith("climate.")][:3]

    if lights:
        cards.append({"type": "custom:mushroom-title-card", "title": "💡 Verlichting"})
        for light in lights:
            cards.append({"type": "custom:mushroom-light-card", "entity": light["entity_id"], "use_light_color": True})

    if climate:
        cards.append({"type": "custom:mushroom-title-card", "title": "🌡️ Klimaat"})
        for c in climate:
            cards.append({"type": "custom:mushroom-climate-card", "entity": c["entity_id"]})

    if switches:
        cards.append({"type": "custom:mushroom-title-card", "title": "🔌 Apparaten"})
        for sw in switches:
            cards.append({"type": "custom:mushroom-entity-card", "entity": sw["entity_id"], "tap_action": {"action": "toggle"}})

    return {
        "title": title,
        "views": [{
            "title": "Overzicht",
            "path": "home",
            "icon": "mdi:view-dashboard",
            "type": "sections",
            "sections": [{"type": "grid", "cards": cards}]
        }]
    }


def build_area_based_dashboard(title: str) -> Dict[str, Any]:
    states = safe_get_states()
    areas = get_area_registry()
    entity_registry = get_entity_registry()

    entity_to_area: Dict[str, str] = {}
    area_names: Dict[str, str] = {}

    for area in areas:
        area_id = area.get("area_id", "")
        area_name = area.get("name", "")
        if area_id and area_name:
            area_names[area_id] = area_name

    for ent_reg in entity_registry:
        entity_id = ent_reg.get("entity_id", "")
        area_id = ent_reg.get("area_id", "")
        if entity_id and area_id:
            entity_to_area[entity_id] = area_id

    entities_by_area: Dict[str, List[Dict[str, Any]]] = {}
    entities_without_area: List[Dict[str, Any]] = []

    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id:
            continue
        area_id = entity_to_area.get(entity_id)
        if area_id:
            entities_by_area.setdefault(area_id, []).append(state)
        else:
            entities_without_area.append(state)

    views: List[Dict[str, Any]] = []

    home_cards: List[Dict[str, Any]] = [{
        "type": "custom:mushroom-title-card",
        "title": "Hallo! 👋",
        "subtitle": "{{ now().strftime('%-d %B %Y') }}"
    }]

    chips: List[Dict[str, Any]] = []
    persons = [e for e in states if (e.get("entity_id", "") or "").startswith("person.")]
    lights = [e for e in states if (e.get("entity_id", "") or "").startswith("light.")]
    if persons:
        chips.append({"type": "entity", "entity": persons[0]["entity_id"], "use_entity_picture": True})
    if lights:
        light_count = len([l for l in lights if (l.get("state") or "") == "on"])
        chips.append({"type": "template", "icon": "mdi:lightbulb-group", "content": f"{light_count} aan", "tap_action": {"action": "none"}})

    power_sensors = [e for e in states if "power" in (e.get("entity_id", "") or "").lower() and "sensor." in (e.get("entity_id", "") or "")]
    if power_sensors:
        chips.append({"type": "entity", "entity": power_sensors[0]["entity_id"]})

    if chips:
        home_cards.append({"type": "custom:mushroom-chips-card", "chips": chips, "alignment": "center"})

    for area_id, area_entities in sorted(entities_by_area.items()):
        area_name = area_names.get(area_id, area_id)

        area_lights = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("light.")]
        area_climate = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("climate.")]
        area_temp = [e for e in area_entities if "temperature" in (e.get("entity_id", "") or "").lower()]

        icon = "mdi:home"
        low = area_name.lower()
        if "woonkamer" in low or "living" in low:
            icon = "mdi:sofa"
        elif "slaapkamer" in low or "bedroom" in low:
            icon = "mdi:bed"
        elif "keuken" in low or "kitchen" in low:
            icon = "mdi:chef-hat"
        elif "badkamer" in low or "bathroom" in low:
            icon = "mdi:shower"
        elif "zolder" in low or "attic" in low:
            icon = "mdi:home-roof"
        elif "kantoor" in low or "office" in low:
            icon = "mdi:desk"
        elif "tuin" in low or "garden" in low:
            icon = "mdi:flower"

        temp_info = ""
        if area_temp:
            temp_info = f"{{{{ states('{area_temp[0]['entity_id']}') }}}}°C"
        elif area_climate:
            temp_info = f"{{{{ state_attr('{area_climate[0]['entity_id']}', 'current_temperature') }}}}°C"

        light_info = ""
        if area_lights:
            on_count = len([l for l in area_lights if (l.get("state") or "") == "on"])
            light_info = f"{on_count}/{len(area_lights)} lampen"

        secondary_text = " | ".join(filter(None, [temp_info, light_info]))

        home_cards.append({
            "type": "custom:mushroom-template-card",
            "primary": area_name,
            "secondary": secondary_text or "Klik voor details",
            "icon": icon,
            "icon_color": "blue",
            "tap_action": {"action": "navigate", "navigation_path": f"#{sanitize_filename(area_name).replace('_', '-')}"},
            "card_mod": {"style": "ha-card { background: rgba(var(--rgb-primary-color), 0.05); }"}
        })

    if entities_without_area:
        home_cards.append({"type": "custom:mushroom-title-card", "title": "Overig"})
        for entity in entities_without_area[:6]:
            entity_id = entity.get("entity_id", "")
            if entity_id.startswith("light."):
                home_cards.append({"type": "custom:mushroom-light-card", "entity": entity_id, "use_light_color": True})
            elif entity_id.startswith("switch."):
                home_cards.append({"type": "custom:mushroom-entity-card", "entity": entity_id, "tap_action": {"action": "toggle"}})

    views.append({
        "title": "Home",
        "path": "home",
        "icon": "mdi:home",
        "type": "sections",
        "sections": [{"type": "grid", "cards": home_cards, "column_span": 1}]
    })

    for area_id, area_entities in sorted(entities_by_area.items()):
        area_name = area_names.get(area_id, area_id)
        area_path = sanitize_filename(area_name).replace("_", "-")

        area_cards: List[Dict[str, Any]] = [{
            "type": "custom:mushroom-title-card",
            "title": area_name,
            "subtitle": "{{ now().strftime('%H:%M') }}"
        }]

        area_lights = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("light.")]
        area_switches = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("switch.")]
        area_climate = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("climate.")]
        area_covers = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("cover.")]
        area_sensors = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("sensor.")]
        area_media = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("media_player.")]

        if area_lights:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "💡 Verlichting"})
            for light in area_lights:
                area_cards.append({
                    "type": "custom:mushroom-light-card",
                    "entity": light["entity_id"],
                    "use_light_color": True,
                    "show_brightness_control": True,
                    "show_color_control": True,
                    "collapsible_controls": True
                })

        if area_climate:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🌡️ Klimaat"})
            for climate in area_climate:
                area_cards.append({
                    "type": "custom:mushroom-climate-card",
                    "entity": climate["entity_id"],
                    "show_temperature_control": True,
                    "collapsible_controls": True
                })

        if area_covers:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🪟 Raamdecoratie"})
            for cover in area_covers:
                area_cards.append({
                    "type": "custom:mushroom-cover-card",
                    "entity": cover["entity_id"],
                    "show_buttons_control": True,
                    "show_position_control": True,
                    "collapsible_controls": True
                })

        if area_media:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🎵 Media"})
            for media in area_media:
                area_cards.append({
                    "type": "custom:mushroom-media-player-card",
                    "entity": media["entity_id"],
                    "use_media_info": True,
                    "show_volume_level": True,
                    "collapsible_controls": True
                })

        if area_switches:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🔌 Apparaten"})
            for sw in area_switches:
                area_cards.append({"type": "custom:mushroom-entity-card", "entity": sw["entity_id"], "tap_action": {"action": "toggle"}})

        temp_sensors = [s for s in area_sensors if "temperature" in (s.get("entity_id", "") or "").lower()]
        humidity_sensors = [s for s in area_sensors if "humidity" in (s.get("entity_id", "") or "").lower()]
        if temp_sensors or humidity_sensors:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "📊 Metingen"})
            for temp in temp_sensors[:3]:
                area_cards.append({"type": "custom:mushroom-entity-card", "entity": temp["entity_id"], "icon": "mdi:thermometer"})
            for hum in humidity_sensors[:3]:
                area_cards.append({"type": "custom:mushroom-entity-card", "entity": hum["entity_id"], "icon": "mdi:water-percent"})

        if len(area_cards) == 1:
            area_cards.append({"type": "markdown", "content": f"# {area_name}\n\n✅ Nog geen devices toegevoegd aan deze ruimte.\n\nVoeg devices toe via Instellingen → Apparaten & Diensten."})

        views.append({
            "title": area_name,
            "path": area_path,
            "icon": "mdi:door",
            "type": "sections",
            "sections": [{"type": "grid", "cards": area_cards, "column_span": 1}]
        })

    return {"title": title, "views": views}


def build_dashboard_yaml(dashboard_title: str) -> Dict[str, Any]:
    return build_area_based_dashboard(dashboard_title)


# -----------------------------------------------------------------------------
# API endpoints
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index() -> Response:
    html = HTML_PAGE.replace("__APP_NAME__", APP_NAME).replace("__APP_VERSION__", APP_VERSION)
    return Response(html, mimetype="text/html")


@app.route("/api/setup", methods=["POST"])
def api_setup():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    data = request.json or {}
    preset = (data.get("preset") or "midnight_pro").strip()
    density = (data.get("density") or "comfy").strip()

    steps: List[str] = []

    try:
        ok_lovelace, msg_lovelace = ensure_lovelace_config()
        steps.append(f"✅ {msg_lovelace}" if ok_lovelace else f"⚠️ {msg_lovelace}")

        steps.append(install_mushroom())
        steps.append(ensure_mushroom_resource())
        steps.append(install_dashboard_theme(preset, density))
        steps.append(try_set_theme_auto())

        ha_call_service("homeassistant", "reload_core_config", {})
        steps.append("✅ Core config herladen")
        time.sleep(1)
        ha_call_service("lovelace", "reload", {})
        steps.append("✅ Lovelace herladen")

        return jsonify({"ok": True, "steps": steps}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps}), 500


@app.route("/api/create_dashboards", methods=["POST"])
def api_create_dashboards():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    data = request.json or {}
    base_title = (data.get("base_title") or "").strip()
    dashboard_type = (data.get("dashboard_type") or "area_based").strip()

    if not base_title:
        return jsonify({"success": False, "error": "Naam ontbreekt."}), 400

    if dashboard_type == "simple":
        dash = build_simple_single_page_dashboard(base_title)
    else:
        dash = build_area_based_dashboard(base_title)

    code = safe_yaml_dump(dash)
    fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(base_title)}.yaml")
    write_text_file(os.path.join(DASHBOARDS_PATH, fn), code)

    reg_msg = register_dashboard_in_lovelace(fn, base_title)

    try:
        ha_call_service("homeassistant", "reload_core_config", {})
        time.sleep(2)
        ha_call_service("lovelace", "reload", {})
    except Exception as e:
        print(f"⚠️ Reload warning: {e}")

    return jsonify({
        "success": True,
        "filename": fn,
        "title": base_title,
        "type": dashboard_type,
        "register": reg_msg,
        "message": f"Dashboard '{base_title}' aangemaakt! ({len(dash.get('views', []))} pagina's)"
    }), 200


@app.route("/api/dashboards", methods=["GET"])
def api_list_dashboards():
    files = list_yaml_files(DASHBOARDS_PATH)
    items = [{"name": Path(f).stem, "filename": f} for f in files]
    return jsonify(items), 200


@app.route("/api/download", methods=["GET"])
def api_download():
    filename = (request.args.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename ontbreekt"}), 400

    safe = os.path.basename(filename)
    path = os.path.join(DASHBOARDS_PATH, safe)
    if not os.path.exists(path):
        return jsonify({"error": "bestand niet gevonden"}), 404

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content, mimetype="text/plain; charset=utf-8")


@app.route("/api/reload_lovelace", methods=["POST"])
def api_reload_lovelace():
    try:
        ha_call_service("lovelace", "reload", {})
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ✅ Fix 4: Update api_config endpoint met betere diagnostics
@app.route("/api/config", methods=["GET"])
def api_config():
    ok, msg = conn.probe(force=True)

    response_data = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "ha_ok": bool(ok),
        "ha_message": msg,
        "active_mode": conn.active_mode,
        "active_base_url": conn.active_base_url,

        "server_time": datetime.now().isoformat(timespec="seconds"),
        "mushroom_installed": mushroom_installed(),
        "theme_file_exists": os.path.exists(DASHBOARD_THEME_FILE),
        "token_debug": conn.token_debug,
    }

    if not ok:
        response_data["probe_attempts"] = conn.probe_attempts
        response_data["detailed_errors"] = [
            {
                "url": attempt.get("url"),
                "mode": attempt.get("mode"),
                "error": attempt.get("error"),
                "status_code": attempt.get("status_code"),
                "content_type": attempt.get("content_type"),
                "response_preview": (attempt.get("response_text", "") or "")[:200],
            }
            for attempt in conn.probe_attempts
        ]

    return jsonify(response_data), 200


# -----------------------------------------------------------------------------
# HTML UI
# -----------------------------------------------------------------------------
HTML_PAGE = """<!DOCTYPE html>
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
          <p class="text-gray-600 mt-2">Maak multi-page dashboards (Home + per ruimte) met Mushroom cards.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">__APP_VERSION__</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinden…</span>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
        <div class="bg-white border border-slate-200 rounded-xl p-4">
          <div class="font-semibold">Verbinding</div>
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
      </div>

      <div class="mt-6 border border-slate-200 rounded-2xl p-5">
        <h2 class="text-xl font-bold text-slate-900">Maak jouw dashboard</h2>

        <div class="mt-4">
          <label class="block text-base font-semibold text-gray-700 mb-2">Dashboard Type</label>
          <select id="dashboardType" class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none">
            <option value="area_based">📍 Per Ruimte (Home + Woonkamer + Slaapkamer...)</option>
            <option value="simple">📊 Simpel (Alles op 1 pagina)</option>
          </select>
          <div class="text-xs text-slate-500 mt-1">
            <span id="dashboardTypeHelp">Multi-page dashboard met Home overzicht + per ruimte details</span>
          </div>
        </div>

        <div class="mt-4">
          <label class="block text-base font-semibold text-gray-700 mb-2">Naam Dashboard</label>
          <input type="text" id="dashName" placeholder="bijv. Mijn Thuis"
                 class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none">
        </div>

        <div class="mt-3 flex flex-col sm:flex-row gap-3">
          <button onclick="createMine()" class="w-full sm:w-auto bg-gradient-to-r from-indigo-600 to-purple-600 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:from-indigo-700 hover:to-purple-700 shadow-lg">
            🎨 Maak mijn dashboard
          </button>
        </div>
      </div>

      <div id="dashboardsList" class="bg-white rounded-2xl shadow-2xl p-6 sm:p-8 mt-6 hidden">
        <h2 class="text-2xl font-bold text-gray-800 mb-4">📚 Dashboards</h2>
        <div id="dashboardsContent" class="space-y-3"></div>
      </div>

    </div>
  </div>

<script>
  // ✅ Fix voor Ingress: API_BASE moet de huidige path prefix gebruiken
  // Dit voorkomt dat fetch('/api/config') per ongeluk naar Home Assistant core gaat.
  var API_BASE = (function() {
    var p = window.location.pathname || '/';
    // remove trailing filename if any, keep directory
    if (!p.endsWith('/')) {
      p = p.substring(0, p.lastIndexOf('/') + 1);
    }
    // Ingress pad eindigt vaak op "/"
    if (p.endsWith('/')) p = p.slice(0, -1);
    return p;
  })();

  function setStatus(text, color) {
    color = color || 'gray';
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }

  function setCheck(id, ok, msg) {
    var el = document.getElementById(id);
    el.textContent = (ok ? '✅ ' : '❌ ') + msg;
    el.className = 'text-sm mt-1 ' + (ok ? 'text-green-700' : 'text-red-700');
  }

  // ✅ Belangrijk: veilig JSON parsen om "Unexpected non-whitespace..." te fixen
  async function fetchJsonSafe(url, opts) {
    var res = await fetch(url, opts || {});
    var text = await res.text();

    // Probeer JSON te parsen, maar vang errors af en geef bruikbare info terug
    try {
      var data = JSON.parse(text);
      return { ok: res.ok, status: res.status, data: data, raw: text };
    } catch (e) {
      console.error('❌ Non-JSON response for', url, 'status', res.status, 'preview:', text.substring(0, 300));
      return {
        ok: false,
        status: res.status,
        data: null,
        raw: text,
        parse_error: e.message
      };
    }
  }

  async function runSetup() {
    try {
      setStatus('Setup...', 'yellow');
      var preset = 'midnight_pro';
      var density = 'comfy';
      var r = await fetchJsonSafe(API_BASE + '/api/setup', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ preset: preset, density: density })
      });

      if (!r.ok || !r.data || !r.data.ok) {
        alert('❌ Setup mislukt: ' + (r.data && r.data.error ? r.data.error : (r.parse_error || 'Non-JSON response')));
        setStatus('Setup mislukt', 'red');
        return;
      }

      alert('✅ Setup klaar!\\n\\n' + (r.data.steps ? r.data.steps.join('\\n') : ''));
      setStatus('Setup klaar', 'green');
      init();
    } catch (e) {
      console.error(e);
      alert('❌ Setup error: ' + e.message);
      setStatus('Setup error', 'red');
    }
  }

  async function createMine() {
    var base_title = document.getElementById('dashName').value.trim();
    if (!base_title) {
      alert('❌ Vul een naam in.');
      return;
    }

    try {
      setStatus('Dashboard maken...', 'yellow');

      var dashboardType = document.getElementById('dashboardType').value || 'area_based';

      var r = await fetchJsonSafe(API_BASE + '/api/create_dashboards', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          base_title: base_title,
          dashboard_type: dashboardType
        })
      });

      if (!r.ok || !r.data || !r.data.success) {
        alert('❌ Maken mislukt: ' + (r.data && r.data.error ? r.data.error : (r.parse_error || 'Non-JSON response')));
        setStatus('Maken mislukt', 'red');
        return;
      }

      setStatus('Dashboard gereed!', 'green');
      alert('✅ Dashboard aangemaakt!\\n\\n' + r.data.message + '\\n\\n➡️ Ververs je browser (F5) en check de sidebar!');
    } catch (e) {
      console.error(e);
      setStatus('Maken mislukt', 'red');
      alert('❌ Maken mislukt: ' + e.message);
    }
  }

  // ✅ Fix 5: Update JavaScript init() in HTML met betere error display
  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      var cfgRes = await fetchJsonSafe(API_BASE + '/api/config');

      // Als response geen JSON is, fixen we exact jouw foutmelding met duidelijke output
      if (!cfgRes.data) {
        setStatus('Verbinding mislukt', 'red');
        setCheck('chkEngine', false, 'Kan niet verbinden: ' + (cfgRes.parse_error || 'Non-JSON response'));
        setCheck('chkCards', false, 'Kan niet verbinden');
        setCheck('chkStyle', false, 'Kan niet verbinden');
        return;
      }

      var cfg = cfgRes.data;

      if (cfg.ha_ok) {
        setStatus('Verbonden (' + (cfg.active_mode || 'ok') + ')', 'green');
        setCheck('chkEngine', true, 'OK');
      } else {
        setStatus('Geen verbinding', 'red');

        var errorMsg = cfg.ha_message || 'Geen verbinding';
        if (errorMsg.length > 100) {
          errorMsg = errorMsg.substring(0, 100) + '...';
        }

        setCheck('chkEngine', false, errorMsg);

        console.error('Connection failed:', cfg.ha_message);
        if (cfg.detailed_errors) console.error('Detailed errors:', cfg.detailed_errors);
        if (cfg.probe_attempts) console.error('Probe attempts:', cfg.probe_attempts);
      }

      setCheck('chkCards', true, cfg.mushroom_installed ? 'Al geïnstalleerd' : 'Klaar om te installeren');
      setCheck('chkStyle', true, cfg.theme_file_exists ? 'Al aanwezig' : 'Klaar om te installeren');

    } catch (e) {
      console.error('Init error:', e);
      setStatus('Verbinding mislukt', 'red');
      setCheck('chkEngine', false, 'Kan niet verbinden: ' + e.message);
      setCheck('chkCards', false, 'Kan niet verbinden');
      setCheck('chkStyle', false, 'Kan niet verbinden');
    }
  }

  init();
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    ensure_dir(DASHBOARDS_PATH)
    ensure_dir(WWW_PATH)
    ensure_dir(COMMUNITY_PATH)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8099")), debug=False)
