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
# In HA add-on context: /config is the HA config directory.
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

# Latest releases ZIP (Mushroom) - can be overridden in options.json
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
    files = []
    for fn in os.listdir(folder):
        if fn.lower().endswith((".yaml", ".yml")):
            files.append(fn)
    return sorted(files)


def safe_yaml_dump(data: Any) -> str:
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _read_options_json() -> Dict[str, Any]:
    # Add-on options, if present.
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

    # ✅ Fix 1: Update _test_connection with better error handling
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
                    # ✅ Check of response überhaupt JSON is
                    content_type = r.headers.get("Content-Type", "")
                    if "application/json" not in content_type:
                        debug["error"] = f"Response is geen JSON (Content-Type: {content_type})"
                        debug["response_text"] = r.text[:500]
                        return False, "Geen JSON response", debug

                    # ✅ Probeer JSON te parsen
                    data = r.json()
                    debug["response_message"] = data.get("message", "")
                    debug["response_data"] = str(data)[:200]
                    return True, "OK", debug

                except json.JSONDecodeError as e:
                    debug["json_error"] = str(e)
                    debug["response_text"] = r.text[:500]
                    debug["error"] = f"JSON parse error: {str(e)}"

                    # ✅ Check of het misschien HTML is (login page)
                    if r.text.strip().startswith("<"):
                        debug["error"] = "Response is HTML (mogelijk login page)"
                        return False, "HTML response (geen API)", debug

                    return False, f"Ongeldige JSON: {str(e)[:50]}", debug
                except Exception as e:
                    debug["parse_error"] = str(e)
                    debug["response_text"] = r.text[:500]
                    return False, f"Parse error: {str(e)[:50]}", debug

            if r.status_code == 401:
                debug["error"] = "Unauthorized - token werkt niet"
                debug["response_text"] = r.text[:300]
                return False, "Token ongeldig (401)", debug
            if r.status_code == 403:
                debug["error"] = "Forbidden - geen toegang"
                debug["response_text"] = r.text[:300]
                return False, "Geen toegang (403)", debug

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

    # ✅ Fix 3: Update probe with better error reporting
    def probe(self, force: bool = False) -> Tuple[bool, str]:
        if self.active_base_url and self.active_token and not force:
            return True, f"cached:{self.active_mode}"

        self.refresh_tokens()
        self.probe_attempts = []

        attempts: List[Tuple[str, str, str]] = []

        if self.user_token:
            for url in HA_URLS:
                mode = "supervisor" if "supervisor" in url else "direct"
                attempts.append((url, self.user_token, mode))

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
                "5. Plak in add-on configuratie als 'access_token'\n\n"
                f"Debug info:\n{json.dumps(self.token_debug, indent=2)}"
            )
            self.last_probe = msg
            return False, msg

        print(f"\n🔍 Probing {len(attempts)} connection attempts...")

        all_errors = []

        for url, token, mode in attempts:
            success, message, debug = self._test_connection(url, token, mode)
            self.probe_attempts.append(debug)
            print(f"  {'✓' if success else '✗'} {mode:12} {url:35} → {message}")

            if not success:
                error_detail = debug.get("error", message)
                all_errors.append(f"{mode} @ {url}: {error_detail}")

            if success:
                self.active_base_url = url
                self.active_token = token
                self.active_mode = mode
                self.last_probe = "ok"
                print(f"  ✅ Using: {mode} via {url}\n")
                return True, f"OK via {mode} ({url})"

        error_msg = "❌ Alle verbindingen gefaald!\n\n"
        error_msg += "Geprobeerde verbindingen:\n"
        for err in all_errors:
            error_msg += f"  • {err}\n"

        error_msg += "\n💡 Veelvoorkomende oorzaken:\n"
        error_msg += "1. Token is niet correct of verlopen\n"
        error_msg += "2. Home Assistant is niet bereikbaar\n"
        error_msg += "3. Firewall/netwerk probleem\n"
        error_msg += "4. Token heeft niet genoeg rechten\n\n"

        error_msg += "🔧 Oplossing:\n"
        error_msg += "1. Maak nieuwe Long-Lived Access Token in HA\n"
        error_msg += "2. Zet token in add-on opties: access_token\n"
        error_msg += "3. Herstart de add-on\n"
        error_msg += "4. Check add-on logs voor details\n"

        self.last_probe = error_msg
        print(error_msg)
        return False, error_msg

    # ✅ Fix 4: Improved request method with response validation logging
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

            print(f"📡 {method} {path} → {r.status_code} ({r.headers.get('Content-Type', 'unknown')})")

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
    """Haal entity registry op (voor area_id mapping)"""
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
# ✅ Fix 2: Update mushroom_installed check (multi paths + JS scan)
def mushroom_installed() -> bool:
    possible_paths = [
        os.path.join(MUSHROOM_PATH, "dist"),
        os.path.join(MUSHROOM_PATH, "build"),
        MUSHROOM_PATH
    ]

    for check_path in possible_paths:
        if os.path.exists(check_path):
            try:
                all_files: List[str] = []
                for root, _dirs, files in os.walk(check_path):
                    all_files.extend([f for f in files if f.endswith(".js")])
                if all_files:
                    print(f"✓ Mushroom JS gevonden: {len(all_files)} files in {check_path}")
                    return True
            except Exception as e:
                print(f"Check error in {check_path}: {e}")

    return False


# ✅ Fix 1: Update download_and_extract_zip (temp extract + move)
def download_and_extract_zip(url: str, target_dir: str):
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


# ✅ Fix 3: ensure_mushroom_resource with CDN fallback
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
# Theme (simple placeholder theme installer)
# -----------------------------------------------------------------------------
def install_dashboard_theme(preset: str, density: str) -> str:
    ensure_dir(DASHBOARD_THEME_DIR)
    # Minimal theme content; you can expand this.
    content = f"""# Dashboard Maker Theme
dashboard_maker:
  modes:
    light:
      primary-color: "#4f46e5"
    dark:
      primary-color: "#a78bfa"
  dashboard_density: "{density}"
  preset: "{preset}"
"""
    write_text_file(DASHBOARD_THEME_FILE, content)
    return "✅ Theme geïnstalleerd"


def try_set_theme_auto() -> str:
    # Setting the theme automatically via service is optional; keep best-effort.
    try:
        ha_call_service("frontend", "set_theme", {"name": "dashboard_maker", "mode": "auto"})
        return "✅ Theme geactiveerd (auto)"
    except Exception as e:
        print(f"try_set_theme_auto warning: {e}")
        return "⚠️ Theme activeren niet gelukt (best effort)"


# -----------------------------------------------------------------------------
# configuration.yaml lovelace helpers (backup + ensure + validation)
# -----------------------------------------------------------------------------
def backup_configuration_yaml() -> Optional[str]:
    """Maak backup van configuration.yaml"""
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
    """Zorgt dat lovelace config correct staat in configuration.yaml"""
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
        content = ""

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
        print("➕ Lovelace sectie toegevoegd aan configuration.yaml")
    else:
        if lovelace.get("mode") != "yaml":
            if not backup_path and os.path.exists(config_yaml_path):
                backup_path = backup_configuration_yaml()

            lovelace["mode"] = "yaml"
            needs_update = True
            print("✏️ Lovelace mode aangepast naar 'yaml'")

        if not isinstance(lovelace.get("dashboards"), dict):
            lovelace["dashboards"] = {}
            needs_update = True
            print("➕ Dashboards sectie toegevoegd")

    if needs_update:
        try:
            with open(config_yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            msg = "✅ configuration.yaml bijgewerkt"
            if backup_path:
                msg += f" (backup: {os.path.basename(backup_path)})"
            print(msg)
            return True, msg
        except Exception as e:
            return False, f"Kan configuration.yaml niet schrijven: {e}"

    return True, "Lovelace config al correct"


def register_dashboard_in_lovelace(filename: str, title: str, editable: bool = False) -> str:
    """Registreer dashboard in configuration.yaml met auto-setup van lovelace sectie
    (editable param wordt geaccepteerd maar in YAML-mode is dit gewoon een YAML dashboard)
    """
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

    lovelace = config.get("lovelace", {})
    dashboards = lovelace.get("dashboards", {})

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

        print(f"✅ Dashboard geregistreerd: {dashboard_key} -> {title}")
        print(f"   📁 Bestand: dashboards/{filename}")
        print("   📌 Sidebar: enabled")
        return f"Dashboard '{title}' geregistreerd als '{dashboard_key}'"
    except Exception as e:
        return f"Schrijven gefaald: {e}"


def validate_configuration_yaml() -> Tuple[bool, str, Dict[str, Any]]:
    """Valideer configuration.yaml structuur"""
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    result = {
        "exists": False,
        "readable": False,
        "valid_yaml": False,
        "has_lovelace": False,
        "lovelace_mode": None,
        "dashboard_count": 0,
        "errors": []
    }

    if not os.path.exists(config_yaml_path):
        result["errors"].append("configuration.yaml bestaat niet")
        return False, "Configuration.yaml niet gevonden", result

    result["exists"] = True

    try:
        with open(config_yaml_path, "r", encoding="utf-8") as f:
            content = f.read()
        result["readable"] = True
    except Exception as e:
        result["errors"].append(f"Niet leesbaar: {e}")
        return False, "Kan configuration.yaml niet lezen", result

    try:
        config = yaml.safe_load(content) or {}
        result["valid_yaml"] = True
    except Exception as e:
        result["errors"].append(f"Ongeldige YAML: {e}")
        return False, "Ongeldige YAML syntax", result

    if isinstance(config.get("lovelace"), dict):
        result["has_lovelace"] = True
        lovelace = config["lovelace"]
        result["lovelace_mode"] = lovelace.get("mode")

        if isinstance(lovelace.get("dashboards"), dict):
            result["dashboard_count"] = len(lovelace["dashboards"])

    if not result["has_lovelace"]:
        return False, "Lovelace sectie ontbreekt", result

    if result["lovelace_mode"] != "yaml":
        result["errors"].append(f"Lovelace mode is '{result['lovelace_mode']}' (moet 'yaml' zijn)")
        return False, "Lovelace mode niet correct", result

    return True, "Configuration.yaml is correct", result


# -----------------------------------------------------------------------------
# Dashboard builders
# -----------------------------------------------------------------------------
def build_simple_single_page_dashboard(title: str) -> Dict[str, Any]:
    """Simpel single-page dashboard voor beginners"""
    states = safe_get_states()

    cards: List[Dict[str, Any]] = []
    cards.append({
        "type": "custom:mushroom-title-card",
        "title": title,
        "subtitle": "{{ now().strftime('%d %B %Y') }}"
    })

    lights = [e for e in states if (e.get("entity_id", "") or "").startswith("light.")][:8]
    switches = [e for e in states if (e.get("entity_id", "") or "").startswith("switch.")][:6]
    climate = [e for e in states if (e.get("entity_id", "") or "").startswith("climate.")][:3]

    if lights:
        cards.append({"type": "custom:mushroom-title-card", "title": "💡 Verlichting"})
        for light in lights:
            cards.append({
                "type": "custom:mushroom-light-card",
                "entity": light["entity_id"],
                "use_light_color": True
            })

    if climate:
        cards.append({"type": "custom:mushroom-title-card", "title": "🌡️ Klimaat"})
        for c in climate:
            cards.append({
                "type": "custom:mushroom-climate-card",
                "entity": c["entity_id"]
            })

    if switches:
        cards.append({"type": "custom:mushroom-title-card", "title": "🔌 Apparaten"})
        for sw in switches:
            cards.append({
                "type": "custom:mushroom-entity-card",
                "entity": sw["entity_id"],
                "tap_action": {"action": "toggle"}
            })

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


# ✅ Fix 1: New Area-Based multi-page builder (as requested)
def build_area_based_dashboard(title: str) -> Dict[str, Any]:
    """Bouwt een multi-page dashboard gebaseerd op areas/kamers"""
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
            if area_id not in entities_by_area:
                entities_by_area[area_id] = []
            entities_by_area[area_id].append(state)
        else:
            entities_without_area.append(state)

    views: List[Dict[str, Any]] = []

    # VIEW 1: HOME/OVERVIEW
    home_cards: List[Dict[str, Any]] = []

    home_cards.append({
        "type": "custom:mushroom-title-card",
        "title": "Hallo! 👋",
        "subtitle": "{{ now().strftime('%-d %B %Y') }}"
    })

    chips: List[Dict[str, Any]] = []
    persons = [e for e in states if (e.get("entity_id", "") or "").startswith("person.")]
    lights = [e for e in states if (e.get("entity_id", "") or "").startswith("light.")]

    if persons:
        chips.append({"type": "entity", "entity": persons[0]["entity_id"], "use_entity_picture": True})
    if lights:
        light_count = len([l for l in lights if (l.get("state") or "") == "on"])
        chips.append({
            "type": "template",
            "icon": "mdi:lightbulb-group",
            "content": f"{light_count} aan",
            "tap_action": {"action": "none"}
        })

    power_sensors = [
        e for e in states
        if "power" in (e.get("entity_id", "") or "").lower() and "sensor." in (e.get("entity_id", "") or "")
    ]
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
            "tap_action": {
                "action": "navigate",
                "navigation_path": f"#{sanitize_filename(area_name).replace('_', '-')}"
            },
            "card_mod": {
                "style": "ha-card { background: rgba(var(--rgb-primary-color), 0.05); }"
            }
        })

    if entities_without_area:
        home_cards.append({"type": "custom:mushroom-title-card", "title": "Overig"})

        for entity in entities_without_area[:6]:
            entity_id = entity.get("entity_id", "")
            if entity_id.startswith("light."):
                home_cards.append({
                    "type": "custom:mushroom-light-card",
                    "entity": entity_id,
                    "use_light_color": True
                })
            elif entity_id.startswith("switch."):
                home_cards.append({
                    "type": "custom:mushroom-entity-card",
                    "entity": entity_id,
                    "tap_action": {"action": "toggle"}
                })

    views.append({
        "title": "Home",
        "path": "home",
        "icon": "mdi:home",
        "type": "sections",
        "sections": [{
            "type": "grid",
            "cards": home_cards,
            "column_span": 1
        }]
    })

    # VIEW 2+: per area
    for area_id, area_entities in sorted(entities_by_area.items()):
        area_name = area_names.get(area_id, area_id)
        area_path = sanitize_filename(area_name).replace("_", "-")

        area_cards: List[Dict[str, Any]] = []

        area_cards.append({
            "type": "custom:mushroom-title-card",
            "title": area_name,
            "subtitle": "{{ now().strftime('%H:%M') }}"
        })

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
            for switch in area_switches:
                area_cards.append({
                    "type": "custom:mushroom-entity-card",
                    "entity": switch["entity_id"],
                    "tap_action": {"action": "toggle"}
                })

        temp_sensors = [s for s in area_sensors if "temperature" in (s.get("entity_id", "") or "").lower()]
        humidity_sensors = [s for s in area_sensors if "humidity" in (s.get("entity_id", "") or "").lower()]

        if temp_sensors or humidity_sensors:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "📊 Metingen"})

            for temp in temp_sensors[:3]:
                area_cards.append({
                    "type": "custom:mushroom-entity-card",
                    "entity": temp["entity_id"],
                    "icon": "mdi:thermometer"
                })

            for hum in humidity_sensors[:3]:
                area_cards.append({
                    "type": "custom:mushroom-entity-card",
                    "entity": hum["entity_id"],
                    "icon": "mdi:water-percent"
                })

        if len(area_cards) == 1:
            area_cards.append({
                "type": "markdown",
                "content": f"# {area_name}\n\n✅ Nog geen devices toegevoegd aan deze ruimte.\n\nVoeg devices toe via Instellingen → Apparaten & Diensten."
            })

        views.append({
            "title": area_name,
            "path": area_path,
            "icon": "mdi:door",
            "type": "sections",
            "sections": [{
                "type": "grid",
                "cards": area_cards,
                "column_span": 1
            }]
        })

    return {"title": title, "views": views}


# ✅ Fix 2: build_comprehensive_demo_dashboard multi-page (as requested)
def build_comprehensive_demo_dashboard(dashboard_title: str) -> Dict[str, Any]:
    """Demo dashboard met Home overview + area pages"""
    dashboard = build_area_based_dashboard(dashboard_title)

    if len(dashboard["views"]) == 1:
        demo_views = [
            {
                "title": "Woonkamer",
                "path": "woonkamer",
                "icon": "mdi:sofa",
                "type": "sections",
                "sections": [{
                    "type": "grid",
                    "cards": [
                        {"type": "custom:mushroom-title-card", "title": "🛋️ Woonkamer", "subtitle": "Demo ruimte"},
                        {"type": "markdown", "content": "# Woonkamer\n\n✨ Dit is een voorbeeld.\n\nVoeg je eigen devices toe!"}
                    ]
                }]
            },
            {
                "title": "Slaapkamer",
                "path": "slaapkamer",
                "icon": "mdi:bed",
                "type": "sections",
                "sections": [{
                    "type": "grid",
                    "cards": [
                        {"type": "custom:mushroom-title-card", "title": "🛏️ Slaapkamer", "subtitle": "Demo ruimte"},
                        {"type": "markdown", "content": "# Slaapkamer\n\n✨ Dit is een voorbeeld.\n\nVoeg je eigen devices toe!"}
                    ]
                }]
            },
            {
                "title": "Zolder",
                "path": "zolder",
                "icon": "mdi:home-roof",
                "type": "sections",
                "sections": [{
                    "type": "grid",
                    "cards": [
                        {"type": "custom:mushroom-title-card", "title": "🏠 Zolder", "subtitle": "Demo ruimte"},
                        {"type": "markdown", "content": "# Zolder\n\n✨ Dit is een voorbeeld.\n\nVoeg je eigen devices toe!"}
                    ]
                }]
            }
        ]
        dashboard["views"].extend(demo_views)

    return dashboard


# ✅ Fix 3: build_dashboard_yaml production uses area-based
def build_dashboard_yaml(dashboard_title: str) -> Dict[str, Any]:
    """Bouwt een volledig multi-page dashboard met area-based views"""
    return build_area_based_dashboard(dashboard_title)


# -----------------------------------------------------------------------------
# (Optional) Storage import placeholder (keeps endpoint compatible)
# -----------------------------------------------------------------------------
def import_dashboard_to_storage(dashboard_key: str, yaml_code: str) -> Tuple[bool, str]:
    """
    Placeholder: echte storage-import is complex en HA-versie-afhankelijk.
    We draaien in YAML-mode; daarom: return False maar met duidelijke uitleg.
    """
    return False, "Storage import not supported in YAML mode (YAML dashboard file is created instead)."


# -----------------------------------------------------------------------------
# API endpoints
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index() -> Response:
    html = HTML_PAGE.replace("__APP_NAME__", APP_NAME).replace("__APP_VERSION__", APP_VERSION)
    return Response(html, mimetype="text/html")


# ✅ Fix 5: api_config endpoint with better error info
@app.route("/api/config", methods=["GET"])
def api_config():
    ok, msg = conn.probe(force=True)

    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    response_data = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "ha_ok": bool(ok),
        "ha_message": msg,
        "active_mode": conn.active_mode,
        "active_base_url": conn.active_base_url,

        "configured_config_path": CONFIGURED_CONFIG_PATH,
        "active_config_path": HA_CONFIG_PATH,
        "dashboards_path": DASHBOARDS_PATH,
        "config_yaml_path": config_yaml_path,
        "config_yaml_exists": os.path.exists(config_yaml_path),
        "config_yaml_writable": os.access(config_yaml_path, os.W_OK) if os.path.exists(config_yaml_path) else False,

        "server_time": datetime.now().isoformat(timespec="seconds"),
        "mushroom_installed": mushroom_installed(),
        "theme_file_exists": os.path.exists(DASHBOARD_THEME_FILE),
        "options_json_found": os.path.exists(ADDON_OPTIONS_PATH),
        "options_json_path": ADDON_OPTIONS_PATH,
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
                "response_preview": attempt.get("response_text", "")[:200]
            }
            for attempt in conn.probe_attempts
        ]

    return jsonify(response_data)


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


@app.route("/api/init_lovelace", methods=["POST"])
def api_init_lovelace():
    """Initialiseer lovelace config in configuration.yaml"""
    try:
        ok, msg = ensure_lovelace_config()

        if ok:
            ha_call_service("homeassistant", "reload_core_config", {})
            return jsonify({
                "success": True,
                "message": msg,
                "config_path": os.path.join(HA_CONFIG_PATH, "configuration.yaml")
            }), 200

        return jsonify({"success": False, "error": msg}), 400

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ✅ Demo dashboard: write file, register, reload in correct order
@app.route("/api/create_demo", methods=["POST"])
def api_create_demo():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    title = "WOW Demo Dashboard"
    dash = build_comprehensive_demo_dashboard(title)
    code = safe_yaml_dump(dash)
    fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(title)}.yaml")
    write_text_file(os.path.join(DASHBOARDS_PATH, fn), code)

    reg_msg = register_dashboard_in_lovelace(fn, title)

    try:
        ha_call_service("homeassistant", "reload_core_config", {})
        print("✅ Core config reloaded")
        time.sleep(1)
        ha_call_service("lovelace", "reload", {})
        print("✅ Lovelace reloaded")
    except Exception as e:
        print(f"⚠️ Reload warning: {e}")

    return jsonify({
        "success": True,
        "filename": fn,
        "register": reg_msg,
        "message": "Dashboard aangemaakt. Herlaad je browser als het niet meteen verschijnt."
    }), 200


# ✅ Fix 5: create dashboards supports dashboard_type (+ edit_mode accepted)
@app.route("/api/create_dashboards", methods=["POST"])
def api_create_dashboards():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    data = request.json or {}
    base_title = (data.get("base_title") or "").strip()
    dashboard_type = (data.get("dashboard_type") or "area_based").strip()
    edit_mode = (data.get("edit_mode") or "editable").strip()

    if not base_title:
        return jsonify({"success": False, "error": "Naam ontbreekt."}), 400

    editable = (edit_mode in ["editable", "hybrid"])

    # Build dashboard
    if dashboard_type == "area_based":
        dash = build_area_based_dashboard(base_title)
    elif dashboard_type == "type_based":
        dash = build_dashboard_yaml(base_title)  # currently same as area-based per Fix 3
    else:
        dash = build_simple_single_page_dashboard(base_title)

    code = safe_yaml_dump(dash)
    dashboard_key = sanitize_filename(base_title).replace("_", "-")

    # In this implementation we always create YAML file (reliable)
    fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(base_title)}.yaml")
    write_text_file(os.path.join(DASHBOARDS_PATH, fn), code)

    reg_msg = register_dashboard_in_lovelace(fn, base_title, editable=editable)

    # Storage import is placeholder (kept for compatibility with your UI)
    import_ok, import_msg = (False, "YAML file created")
    if editable:
        import_ok, import_msg = import_dashboard_to_storage(dashboard_key, code)

    try:
        ha_call_service("homeassistant", "reload_core_config", {})
        time.sleep(2)
        ha_call_service("lovelace", "reload", {})
    except Exception as e:
        print(f"⚠️ Reload warning: {e}")

    return jsonify({
        "success": True,
        "dashboard_key": dashboard_key,
        "filename": fn,
        "title": base_title,
        "type": dashboard_type,
        "editable": editable and import_ok,
        "register": reg_msg,
        "import": import_msg,
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


@app.route("/api/delete_dashboard", methods=["POST"])
def api_delete_dashboard():
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename ontbreekt"}), 400

    safe = os.path.basename(filename)
    path = os.path.join(DASHBOARDS_PATH, safe)
    if not os.path.exists(path):
        return jsonify({"error": "bestand niet gevonden"}), 404

    try:
        os.remove(path)
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reload_lovelace", methods=["POST"])
def api_reload_lovelace():
    try:
        ha_call_service("lovelace", "reload", {})
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# -----------------------------------------------------------------------------
# Debug endpoints
# -----------------------------------------------------------------------------
@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    ok, msg = conn.probe(force=True)
    return jsonify({
        "ok": ok,
        "message": msg,
        "active": {"url": conn.active_base_url, "mode": conn.active_mode},
        "attempts": conn.probe_attempts,
        "token_debug": conn.token_debug,
    }), 200


@app.route("/api/debug/connection", methods=["GET"])
def api_debug_connection():
    """Uitgebreide connection debug info"""
    ok, msg = conn.probe(force=True)

    debug_data = {
        "connection_ok": ok,
        "connection_message": msg,
        "active_connection": {
            "url": conn.active_base_url,
            "mode": conn.active_mode,
            "has_token": bool(conn.active_token),
            "token_length": len(conn.active_token) if conn.active_token else 0
        },
        "token_discovery": conn.token_debug,
        "probe_attempts": conn.probe_attempts,
        "environment": {
            "SUPERVISOR_TOKEN": bool(os.environ.get(SUPERVISOR_TOKEN_ENV)),
            "HOMEASSISTANT_TOKEN": bool(os.environ.get(HOMEASSISTANT_TOKEN_ENV)),
            "HA_CONFIG_PATH": HA_CONFIG_PATH,
            "ADDON_OPTIONS_PATH": ADDON_OPTIONS_PATH,
        },
        "options_json": _read_options_json(),
        "urls_tried": HA_URLS,
    }

    return jsonify(debug_data), 200


@app.route("/api/debug/dashboards", methods=["GET"])
def api_debug_dashboards():
    """Debug endpoint om te zien waarom dashboards niet verschijnen"""
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    debug_info = {
        "config_yaml_exists": os.path.exists(config_yaml_path),
        "config_yaml_path": config_yaml_path,
        "dashboards_path": DASHBOARDS_PATH,
        "dashboards_path_exists": os.path.exists(DASHBOARDS_PATH),
    }

    if os.path.exists(config_yaml_path):
        try:
            with open(config_yaml_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            debug_info["config_yaml_content"] = config
            debug_info["lovelace_config"] = config.get("lovelace", {})
        except Exception as e:
            debug_info["config_yaml_error"] = str(e)

    try:
        files = list_yaml_files(DASHBOARDS_PATH)
        debug_info["dashboard_files"] = files
        debug_info["dashboard_files_count"] = len(files)
    except Exception as e:
        debug_info["dashboard_files_error"] = str(e)

    try:
        debug_info["dashboards_writable"] = os.access(DASHBOARDS_PATH, os.W_OK)
        debug_info["config_writable"] = os.access(config_yaml_path, os.W_OK) if os.path.exists(config_yaml_path) else False
    except Exception as e:
        debug_info["permissions_error"] = str(e)

    return jsonify(debug_info), 200


@app.route("/api/debug/config_yaml", methods=["GET"])
def api_debug_config_yaml():
    """Debug configuration.yaml structuur"""
    valid, msg, details = validate_configuration_yaml()

    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    preview = None
    if os.path.exists(config_yaml_path):
        try:
            with open(config_yaml_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                preview = "".join(lines[:50])
                if len(lines) > 50:
                    preview += f"\n... ({len(lines) - 50} more lines)"
        except Exception as e:
            preview = f"Error reading: {e}"

    return jsonify({
        "valid": valid,
        "message": msg,
        "details": details,
        "path": config_yaml_path,
        "preview": preview
    }), 200


# -----------------------------------------------------------------------------
# HTML UI (with requested dashboard type select + JS createMine update)
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
          <p class="text-gray-600 mt-2">Klik, kies stijl, klaar. Professionele dashboards — zonder technische kennis.</p>
          <p class="text-xs text-gray-500 mt-1">Versie: <span class="font-mono">__APP_VERSION__</span></p>
        </div>
        <div class="flex flex-col items-start sm:items-end gap-2">
          <div id="status" class="text-sm">
            <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
            <span>Verbinden…</span>
          </div>
          <div class="flex gap-2 flex-wrap">
            <button onclick="reloadDashboards()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔄 Vernieuwen</button>
            <button onclick="openDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🧾 Debug</button>
            <button onclick="openDashboardDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔍 Dashboard Check</button>
            <button onclick="openConnectionDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔍 Verbinding Test</button>
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
            <p class="text-slate-600 mt-1">We installeren Mushroom + zetten een premium theme aan (auto licht/donker).</p>
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
          <div class="text-sm text-slate-500 flex items-center"><span id="setupHint">Klik één keer. Wij doen de rest.</span></div>
        </div>

        <!-- ✅ Advanced options / lovelace init -->
        <div class="mt-3 text-sm text-slate-500">
          <details class="cursor-pointer">
            <summary class="font-semibold hover:text-slate-700">⚙️ Geavanceerde opties</summary>
            <div class="mt-2 space-y-2">
              <button onclick="initLovelace()" class="w-full text-left bg-slate-50 border border-slate-200 px-3 py-2 rounded-lg hover:bg-slate-100 text-sm">
                🔧 Initialiseer Lovelace Config
                <div class="text-xs text-slate-500 mt-1">Voegt lovelace sectie toe aan configuration.yaml</div>
              </button>
            </div>
          </details>
        </div>

      </div>

      <div id="step2" class="border border-slate-200 rounded-2xl p-5 mt-4 opacity-50 pointer-events-none">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">Stap 2 — WOW demo</h2>
            <p class="text-slate-600 mt-1">Maak een voorbeeld dashboard zodat je meteen resultaat ziet.</p>
          </div>
          <div class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-700">1 klik</div>
        </div>

        <div class="mt-4 flex flex-col sm:flex-row gap-3">
          <button onclick="createDemo()" class="w-full sm:w-auto bg-slate-900 text-white py-3 px-4 rounded-xl text-lg font-semibold hover:bg-black shadow-lg">
            ✨ Maak demo dashboard
          </button>
        </div>
      </div>

      <div id="step3" class="border border-slate-200 rounded-2xl p-5 mt-4 opacity-50 pointer-events-none">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">Stap 3 — Maak jouw dashboard</h2>
            <p class="text-slate-600 mt-1">Kies type + naam. Wij maken een dashboard met pagina’s.</p>
          </div>
          <div class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-700">Nieuw</div>
        </div>

        <!-- ✅ Fix 4: UI Update met dashboard type -->
        <div class="mt-4">
          <label class="block text-base font-semibold text-gray-700 mb-2">Dashboard Type</label>
          <select id="dashboardType" class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-indigo-500 focus:outline-none">
            <option value="area_based">📍 Per Ruimte (Home + Woonkamer + Slaapkamer...)</option>
            <option value="type_based">🔧 Per Type (Verlichting + Klimaat + Media...)</option>
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
          <button onclick="toggleAdvanced()" class="w-full sm:w-auto bg-white border border-gray-300 text-gray-800 py-3 px-4 rounded-xl text-lg font-semibold hover:bg-gray-100 shadow-lg">
            🔧 Bekijk techniek (optioneel)
          </button>
        </div>

        <div id="advancedPanel" class="hidden mt-4 bg-slate-50 border border-slate-200 rounded-2xl p-4">
          <div class="flex items-center justify-between">
            <div class="font-bold text-slate-900">Technische output</div>
            <button onclick="copyAll()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">📋 Copy</button>
          </div>
          <div class="bg-gray-900 text-green-400 p-3 rounded-xl overflow-x-auto text-xs font-mono mt-3" style="min-height: 120px;">
            <pre id="advancedOut">—</pre>
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
          <button onclick="resetWizard()" class="w-full sm:w-auto bg-white border border-gray-300 text-gray-800 py-3 px-4 rounded-xl text-lg font-semibold hover:bg-gray-100 shadow-lg">
            ➕ Nog een maken
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
  var API_BASE = '';

  function setStatus(text, color) {
    color = color || 'gray';
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }

  function setDot(step, active) {
    var el = document.getElementById(step + 'Dot');
    var dot = el.querySelector('span');
    if (active) {
      el.classList.remove('text-slate-500');
      dot.className = 'w-3 h-3 rounded-full bg-indigo-500 inline-block';
    } else {
      el.classList.add('text-slate-500');
      dot.className = 'w-3 h-3 rounded-full bg-slate-300 inline-block';
    }
  }

  function unlockStep(stepId) {
    var el = document.getElementById(stepId);
    el.classList.remove('opacity-50', 'pointer-events-none');
  }

  function showStep4() {
    document.getElementById('step4').classList.remove('hidden');
    setDot('step4', true);
  }

  function setCheck(id, ok, msg) {
    var el = document.getElementById(id);
    el.textContent = (ok ? '✅ ' : '❌ ') + msg;
    el.className = 'text-sm mt-1 ' + (ok ? 'text-green-700' : 'text-red-700');
  }

  // ✅ Fix: dashboard type help
  document.getElementById('dashboardType').addEventListener('change', function(e) {
    var help = document.getElementById('dashboardTypeHelp');
    var type = e.target.value;

    if (type === 'area_based') {
      help.textContent = 'Multi-page dashboard met Home overzicht + per ruimte details';
    } else if (type === 'type_based') {
      help.textContent = 'Gegroepeerd per device type: Lampen, Klimaat, Schakelaars, etc.';
    } else if (type === 'simple') {
      help.textContent = 'Alles op één pagina, perfect voor beginners';
    }
  });

  // ✅ Fix 7: UI init() with better error display
  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      var cfgRes = await fetch(API_BASE + '/api/config');
      var cfg = await cfgRes.json();

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

      setDot('step1', true);
    } catch (e) {
      console.error('Init error:', e);
      setStatus('Verbinding mislukt', 'red');
      setCheck('chkEngine', false, 'Kan niet verbinden: ' + e.message);
      setCheck('chkCards', false, 'Kan niet verbinden');
      setCheck('chkStyle', false, 'Kan niet verbinden');
    }
  }

  async function runSetup() {
    var preset = document.getElementById('preset').value;
    var density = document.getElementById('density').value;

    document.getElementById('setupHint').textContent = 'Bezig… (Mushroom + theme + auto licht/donker)';
    setCheck('chkCards', true, 'Bezig…');
    setCheck('chkStyle', true, 'Bezig…');

    try {
      var res = await fetch(API_BASE + '/api/setup', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ preset: preset, density: density })
      });

      var data = await res.json();
      if (!res.ok || !data.ok) {
        document.getElementById('setupHint').textContent = 'Dit lukte niet. Probeer opnieuw.';
        alert('❌ Instellen mislukt: ' + (data.error || 'Onbekend'));
        return;
      }

      setCheck('chkCards', true, 'Klaar');
      setCheck('chkStyle', true, 'Klaar');
      document.getElementById('setupHint').textContent = 'Klaar! Je kunt verder.';

      unlockStep('step2');
      unlockStep('step3');
      setDot('step2', true);
      setDot('step3', true);

      alert('✅ Setup klaar!\\n\\n' + (data.steps ? data.steps.join('\\n') : ''));
    } catch (e) {
      document.getElementById('setupHint').textContent = 'Dit lukte niet. Probeer opnieuw.';
      alert('❌ Instellen mislukt: ' + e.message);
    }
  }

  async function initLovelace() {
    if (!confirm('Dit voegt de lovelace configuratie toe aan configuration.yaml.\\n\\nEr wordt automatisch een backup gemaakt.\\n\\nDoorgaan?')) {
      return;
    }

    try {
      setStatus('Lovelace initialiseren...', 'yellow');
      var res = await fetch(API_BASE + '/api/init_lovelace', { method: 'POST' });
      var data = await res.json();

      if (!res.ok || !data.success) {
        alert('❌ Initialisatie mislukt: ' + (data.error || 'Onbekend'));
        setStatus('Initialisatie mislukt', 'red');
        return;
      }

      setStatus('Lovelace geïnitialiseerd', 'green');
      alert('✅ Lovelace configuratie toegevoegd!\\n\\n' + data.message + '\\n\\nPad: ' + data.config_path);
    } catch (e) {
      console.error(e);
      setStatus('Initialisatie mislukt', 'red');
      alert('❌ Initialisatie mislukt.');
    }
  }

  async function createDemo() {
    try {
      setStatus('Demo maken...', 'yellow');
      var res = await fetch(API_BASE + '/api/create_demo', { method: 'POST' });
      var data = await res.json();
      if (!res.ok || !data.success) {
        alert('❌ Demo mislukt: ' + (data.error || 'Onbekend'));
        setStatus('Demo mislukt', 'red');
        return;
      }
      setStatus('Demo gereed!', 'green');

      var msg = '✅ Demo dashboard aangemaakt!\\n\\n';
      msg += '📁 Bestand: ' + data.filename + '\\n';
      msg += '📌 Titel: WOW Demo Dashboard\\n\\n';
      msg += '🔄 BELANGRIJK:\\n';
      msg += '1. Wacht 5 seconden\\n';
      msg += '2. Druk op F5 (of refresh je browser)\\n';
      msg += '3. Check je sidebar voor het nieuwe dashboard\\n\\n';
      msg += '💡 Zie je het niet? Klik rechtsboven op “Dashboard Check”.';

      alert(msg);
      showStep4();
    } catch (e) {
      console.error(e);
      setStatus('Demo mislukt', 'red');
      alert('❌ Demo mislukt.');
    }
  }

  // ✅ Fix 7: Update JavaScript createMine (dashboardType + editMode accepted)
  async function createMine() {
    var base_title = document.getElementById('dashName').value.trim();
    if (!base_title) {
      alert('❌ Vul een naam in.');
      return;
    }

    try {
      setStatus('Dashboard maken...', 'yellow');

      var dashboardType = document.getElementById('dashboardType') ? document.getElementById('dashboardType').value : 'area_based';
      var editMode = document.getElementById('editMode') ? document.getElementById('editMode').value : 'editable';

      var res = await fetch(API_BASE + '/api/create_dashboards', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          base_title: base_title,
          dashboard_type: dashboardType,
          edit_mode: editMode
        })
      });

      var data = await res.json();
      if (!res.ok || !data.success) {
        alert('❌ Maken mislukt: ' + (data.error || 'Onbekend'));
        setStatus('Maken mislukt', 'red');
        return;
      }

      var adv = document.getElementById('advancedPanel');
      if (!adv.classList.contains('hidden')) {
        document.getElementById('advancedOut').textContent = (data.filename || '') + '\\n---\\n' + (data.message || '');
      }

      setStatus('Dashboard gereed!', 'green');

      var msg = '✅ Dashboard aangemaakt!\\n\\n';
      msg += '📁 ' + data.title + '\\n';
      msg += '📄 Type: ' + data.type + '\\n';
      msg += '📑 Pagina\\'s: ' + (data.message.match(/\\d+/)?.[0] || '?') + '\\n';
      if (data.editable) msg += '✏️ Bewerkbaar via UI!\\n';
      msg += '\\n🔄 Ververs je browser (F5) en check de sidebar!\\n';

      alert(msg);
      showStep4();
    } catch (e) {
      console.error(e);
      setStatus('Maken mislukt', 'red');
      alert('❌ Maken mislukt.');
    }
  }

  async function reloadDashboards() {
    try {
      await fetch(API_BASE + '/api/reload_lovelace', { method: 'POST' });
      alert('🔄 Dashboard reload gestart!');
    } catch (e) {
      console.error(e);
    }
  }

  function toggleAdvanced() {
    document.getElementById('advancedPanel').classList.toggle('hidden');
  }

  function copyAll() {
    var text = document.getElementById('advancedOut').textContent || '';
    navigator.clipboard.writeText(text).then(function() {
      alert('📋 Gekopieerd!');
    });
  }

  async function loadDashboards() {
    var response = await fetch(API_BASE + '/api/dashboards');
    var items = await response.json();

    var list = document.getElementById('dashboardsList');
    var content = document.getElementById('dashboardsContent');

    if (!items.length) {
      list.classList.add('hidden');
      alert('Nog geen dashboards opgeslagen!');
      return;
    }

    list.classList.remove('hidden');

    function esc(s) {
      return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    var html = '';
    items.forEach(function(t) {
      html += '<div class="bg-slate-50 border-2 border-slate-200 rounded-xl p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">';
      html += '<div><div class="font-semibold">' + esc(t.name) + '</div>';
      html += '<div class="text-sm text-slate-500 font-mono">' + esc(t.filename) + '</div></div>';
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
    var response = await fetch(API_BASE + '/api/delete_dashboard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename })
    });
    var result = await response.json();
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
    var res = await fetch(API_BASE + '/api/debug/ha');
    var data = await res.json();
    console.log('Debug data:', data);
    alert(JSON.stringify(data, null, 2));
  }

  async function openDashboardDebug() {
    try {
      var res = await fetch(API_BASE + '/api/debug/dashboards');
      var data = await res.json();

      var msg = '🔍 Dashboard Debug Info\\n\\n';
      msg += '📁 Dashboards folder: ' + (data.dashboards_path_exists ? '✓ Exists' : '✗ Missing') + '\\n';
      msg += '📄 Config.yaml: ' + (data.config_yaml_exists ? '✓ Exists' : '✗ Missing') + '\\n';
      msg += '📝 Dashboard files: ' + (data.dashboard_files_count || 0) + '\\n\\n';

      if (data.lovelace_config) {
        msg += '⚙️ Lovelace config:\\n';
        msg += JSON.stringify(data.lovelace_config, null, 2) + '\\n\\n';
      }

      if (data.dashboard_files && data.dashboard_files.length > 0) {
        msg += '📋 Found dashboards:\\n';
        data.dashboard_files.forEach(f => msg += '  - ' + f + '\\n');
      }

      alert(msg);
      console.log('Full debug data:', data);
    } catch (e) {
      console.error(e);
      alert('❌ Debug check failed');
    }
  }

  async function openConnectionDebug() {
    try {
      setStatus('Verbinding testen...', 'yellow');
      var res = await fetch(API_BASE + '/api/debug/connection');
      var data = await res.json();

      console.log('Full connection debug:', data);

      var msg = '🔍 Verbinding Debug\\n\\n';
      msg += 'Status: ' + (data.connection_ok ? '✅ OK' : '❌ FAILED') + '\\n';
      msg += 'Mode: ' + (data.active_connection.mode || 'none') + '\\n';
      msg += 'URL: ' + (data.active_connection.url || 'none') + '\\n\\n';

      if (!data.connection_ok && data.probe_attempts) {
        msg += 'Pogingen:\\n';
        data.probe_attempts.forEach(function(att, i) {
          msg += (i+1) + '. ' + att.mode + ' @ ' + att.url + '\\n';
          msg += '   Error: ' + (att.error || 'unknown') + '\\n';
          if (att.status_code) msg += '   HTTP: ' + att.status_code + '\\n';
        });
      }

      msg += '\\n💡 Check browser console voor volledige details';

      alert(msg);
      setStatus(data.connection_ok ? 'Verbonden' : 'Geen verbinding', data.connection_ok ? 'green' : 'red');
    } catch (e) {
      console.error(e);
      alert('❌ Debug check mislukt: ' + e.message);
    }
  }

  function resetWizard() {
    document.getElementById('dashName').value = '';
    document.getElementById('dashboardsList').classList.add('hidden');
    document.getElementById('step4').classList.add('hidden');
    setDot('step4', false);
    alert('✅ Klaar om nog een dashboard te maken.');
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
