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
import json
import time
import warnings

warnings.filterwarnings("ignore", message="Unverified HTTPS request")


# =========================
# App meta
# =========================
APP_NAME = "Dashboard Maker"
APP_VERSION = "2.6.0-yamlmode"

app = Flask(__name__)


# =========================
# Paths / Defaults
# =========================
ADDON_OPTIONS_PATH = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")
CONFIGURED_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "").strip()  # optional override
HA_CONFIG_PATH = CONFIGURED_CONFIG_PATH if CONFIGURED_CONFIG_PATH else "/config"

DASHBOARDS_PATH = os.path.join(HA_CONFIG_PATH, "dashboards")
WWW_PATH = os.path.join(HA_CONFIG_PATH, "www")
COMMUNITY_PATH = os.path.join(WWW_PATH, "community")
MUSHROOM_PATH = os.path.join(COMMUNITY_PATH, "lovelace-mushroom")

DASHBOARD_THEME_DIR = os.path.join(HA_CONFIG_PATH, "themes", "dashboard_maker")
DASHBOARD_THEME_FILE = os.path.join(DASHBOARD_THEME_DIR, "dashboard_maker.yaml")

# Candidate base urls (direct + supervisor)
HA_URLS = [
    os.environ.get("HA_URL", "").strip(),
    "http://supervisor/core",
    "http://homeassistant:8123",
    "http://localhost:8123",
]
HA_URLS = [u for u in HA_URLS if u]


# =========================
# Helpers: FS / YAML
# =========================
def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def write_text_file(path: str, content: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_yaml_files(folder: str) -> List[str]:
    if not os.path.exists(folder):
        return []
    out: List[str] = []
    for fn in os.listdir(folder):
        if fn.lower().endswith((".yaml", ".yml")):
            out.append(fn)
    return sorted(out)


def sanitize_filename(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_") or "dashboard"


def next_available_filename(folder: str, preferred: str) -> str:
    ensure_dir(folder)
    base = preferred
    if not base.lower().endswith(".yaml"):
        base += ".yaml"
    cand = base
    i = 1
    while os.path.exists(os.path.join(folder, cand)):
        stem = base[:-5]
        cand = f"{stem}_{i}.yaml"
        i += 1
    return cand


def safe_yaml_dump(obj: Any) -> str:
    return yaml.safe_dump(
        obj,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
        indent=2,
    )


# =========================
# Add-on options reader
# =========================
def _read_options_json() -> Dict[str, Any]:
    try:
        if os.path.exists(ADDON_OPTIONS_PATH):
            with open(ADDON_OPTIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


# =========================
# Connection to Home Assistant
# =========================
class HAConnection:
    def __init__(self) -> None:
        self.active_base_url: Optional[str] = None
        self.active_token: Optional[str] = None
        self.active_mode: str = "unknown"

        self.user_token: Optional[str] = None
        self.supervisor_token: Optional[str] = None

        self.last_probe: str = ""
        self.probe_attempts: List[Dict[str, Any]] = []
        self.token_debug: Dict[str, Any] = {}

        self.refresh_tokens()

    def refresh_tokens(self) -> None:
        opts = _read_options_json()
        access_token = (opts.get("access_token") or opts.get("token") or "").strip()

        env_user = (os.environ.get("HOMEASSISTANT_TOKEN") or "").strip()
        env_sup = (os.environ.get("SUPERVISOR_TOKEN") or "").strip()

        self.user_token = access_token or env_user or None
        self.supervisor_token = env_sup or None

        self.token_debug = {
            "options_json_exists": os.path.exists(ADDON_OPTIONS_PATH),
            "options_json_path": ADDON_OPTIONS_PATH,
            "access_token_in_options": bool(access_token),
            "HOMEASSISTANT_TOKEN_env": bool(env_user),
            "SUPERVISOR_TOKEN_env": bool(env_sup),
            "ha_urls": HA_URLS,
        }

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ✅ Fix: improved JSON validation & HTML detection
    def _test_connection(self, url: str, token: Optional[str], mode: str) -> Tuple[bool, str, Dict[str, Any]]:
        debug: Dict[str, Any] = {
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

            r = requests.get(test_url, headers=self._headers(token), timeout=10, verify=False)

            debug["status_code"] = r.status_code
            debug["response_length"] = len(r.text)
            debug["content_type"] = r.headers.get("Content-Type", "")

            if r.status_code == 200:
                try:
                    content_type = r.headers.get("Content-Type", "")
                    if "application/json" not in content_type:
                        debug["error"] = f"Response is geen JSON (Content-Type: {content_type})"
                        debug["response_text"] = r.text[:500]
                        return False, "Geen JSON response", debug

                    data = r.json()
                    debug["response_message"] = data.get("message", "")
                    debug["response_data"] = str(data)[:200]
                    return True, "OK", debug

                except json.JSONDecodeError as e:
                    debug["json_error"] = str(e)
                    debug["response_text"] = r.text[:500]
                    debug["error"] = f"JSON parse error: {str(e)}"

                    if r.text.strip().startswith("<"):
                        debug["error"] = "Response is HTML (mogelijk login page)"
                        return False, "HTML response (login page?)", debug

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

    # ✅ Fix: improved probe reporting
    def probe(self, force: bool = False) -> Tuple[bool, str]:
        if self.active_base_url and self.active_token and not force:
            return True, f"cached:{self.active_mode}"

        self.refresh_tokens()
        self.probe_attempts = []

        attempts: List[Tuple[str, str, str]] = []

        if self.user_token:
            for url in HA_URLS:
                attempts.append((url, self.user_token, "user_token"))

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

    # ✅ Fix: improved request with HTML warning
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
                headers=self._headers(self.active_token or ""),
                json=json_body,
                timeout=timeout,
                verify=False,
            )

            content_type = r.headers.get("Content-Type", "unknown")
            print(f"📡 {method} {path} → {r.status_code} ({content_type})")

            if r.status_code == 200 and "text/html" in content_type:
                print("⚠️ WARNING: Got HTML response instead of JSON")
                print(f"   Response preview: {r.text[:200]}")

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
                        headers=self._headers(self.active_token or ""),
                        json=json_body,
                        timeout=timeout,
                        verify=False,
                    )
            return r

        except requests.exceptions.RequestException as e:
            print(f"❌ Request failed: {method} {path} - {str(e)}")
            raise


conn = HAConnection()


# =========================
# HA API helpers
# =========================
def ha_call_service(domain: str, service: str, data: Dict[str, Any]) -> Tuple[bool, str]:
    try:
        r = conn.request("POST", f"/api/services/{domain}/{service}", json_body=data, timeout=15)
        if r.status_code in (200, 201):
            return True, "OK"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def safe_get_states() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/states", timeout=20)
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "")
            if "application/json" in ct:
                return r.json() or []
    except Exception:
        pass
    return []


def get_area_registry() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/config/area_registry/list", timeout=20)
        if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
            return r.json() or []
    except Exception:
        pass
    return []


def get_entity_registry() -> List[Dict[str, Any]]:
    try:
        r = conn.request("GET", "/api/config/entity_registry/list", timeout=20)
        if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
            return r.json() or []
    except Exception:
        pass
    return []


# =========================
# Mushroom install / checks
# =========================
def mushroom_installed() -> bool:
    possible_paths = [
        os.path.join(MUSHROOM_PATH, "dist"),
        os.path.join(MUSHROOM_PATH, "build"),
        MUSHROOM_PATH,
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


# ✅ Fix: safer zip extraction move
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
        return "✅ Mushroom is al geïnstalleerd"

    # GitHub release zip (stable). If this changes, user can still use HACS.
    url = "https://github.com/piitaya/lovelace-mushroom/releases/latest/download/lovelace-mushroom.zip"
    try:
        download_and_extract_zip(url, COMMUNITY_PATH)
        if mushroom_installed():
            return "✅ Mushroom geïnstalleerd"
        return "⚠️ Mushroom install gedaan, maar JS niet gevonden (check map /config/www/community/lovelace-mushroom)"
    except Exception as e:
        return f"❌ Mushroom install gefaald: {e}"


# ✅ Fix: skip resources endpoint if not available (YAML mode)
def ensure_mushroom_resource() -> str:
    """Probeer Mushroom resource te registreren (werkt niet altijd in YAML mode)"""
    local_url = "/local/community/lovelace-mushroom/dist/mushroom.js"
    cdn_url = "https://unpkg.com/lovelace-mushroom@latest/dist/mushroom.js"

    try:
        r = conn.request("GET", "/api/lovelace/resources", timeout=12)

        if r.status_code == 404:
            print("⚠️ Lovelace resources API niet beschikbaar (YAML mode)")
            return "⚠️ Mushroom resource registratie overgeslagen (YAML mode - voeg handmatig toe)"

        if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
            resources = r.json() or []
            for res in resources:
                url = res.get("url", "")
                if local_url in url or "mushroom" in url:
                    return "✅ Mushroom resource staat goed"

            for url_to_try in [local_url, cdn_url]:
                payload = {"type": "module", "url": url_to_try}
                try:
                    rr = conn.request("POST", "/api/lovelace/resources", json_body=payload, timeout=12)
                    if rr.status_code in (200, 201):
                        source = "lokaal" if "local" in url_to_try else "CDN"
                        return f"✅ Mushroom resource toegevoegd ({source})"
                except Exception as e:
                    print(f"Resource registration via {url_to_try} failed: {e}")
                    continue

    except Exception as e:
        print(f"ensure_mushroom_resource warning: {e}")

    return "⚠️ Mushroom resource moet handmatig toegevoegd worden in configuration.yaml onder lovelace → resources"


# =========================
# Theme install
# =========================
def install_dashboard_theme(preset: str, density: str) -> str:
    ensure_dir(DASHBOARD_THEME_DIR)

    theme_content = f"""# Dashboard Maker Theme
dashboard_maker:
  preset: "{preset}"
  dashboard_density: "{density}"
"""
    write_text_file(DASHBOARD_THEME_FILE, theme_content)

    resources_example = """# Kopieer deze sectie naar je configuration.yaml

lovelace:
  mode: yaml
  resources:
    - url: /local/community/lovelace-mushroom/dist/mushroom.js
      type: module
  dashboards: {}
"""
    resources_file = os.path.join(DASHBOARD_THEME_DIR, "RESOURCES_EXAMPLE.yaml")
    write_text_file(resources_file, resources_example)

    return f"✅ Theme geïnstalleerd (check {resources_file} voor resources voorbeeld)"


# ✅ Fix: better theme activation handling
def try_set_theme_auto() -> str:
    """Probeer theme te activeren (werkt niet altijd)"""
    try:
        r = conn.request(
            "POST",
            "/api/services/frontend/set_theme",
            json_body={"name": "dashboard_maker", "mode": "auto"},
            timeout=12,
        )

        if r.status_code in (200, 201):
            return "✅ Theme geactiveerd (auto)"
        elif r.status_code == 400:
            print(f"⚠️ Theme service niet beschikbaar: {r.text[:200]}")
            return "⚠️ Theme activeren overgeslagen (activeer handmatig in HA profiel)"
        else:
            print(f"⚠️ Theme activeren gefaald: HTTP {r.status_code}")
            return "⚠️ Theme activeren overgeslagen (activeer handmatig)"

    except Exception as e:
        print(f"try_set_theme_auto warning: {e}")
        return "⚠️ Theme activeren overgeslagen (activeer handmatig in HA profiel → Themes)"


# =========================
# configuration.yaml safety + lovelace setup
# =========================
def backup_configuration_yaml() -> Optional[str]:
    """Maak backup van configuration.yaml met timestamp"""
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


# ✅ Fix: preserve existing config, safe_dump with sort_keys False, width to avoid wrapping
def ensure_lovelace_config() -> Tuple[bool, str]:
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")
    backup_path = None

    if os.path.exists(config_yaml_path):
        try:
            with open(config_yaml_path, "r", encoding="utf-8") as f:
                original_content = f.read()
                config = yaml.safe_load(original_content) or {}
        except Exception as e:
            return False, f"Kan configuration.yaml niet lezen: {e}"
    else:
        original_content = ""
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
                yaml.safe_dump(
                    config,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                    width=1000,
                    indent=2,
                )

            msg = "✅ configuration.yaml bijgewerkt"
            if backup_path:
                msg += f" (backup: {os.path.basename(backup_path)})"
            print(msg)
            return True, msg
        except Exception as e:
            return False, f"Kan configuration.yaml niet schrijven: {e}"

    return True, "Lovelace config al correct"


# ✅ Fix: merge dashboards; dashboard key ALWAYS contains hyphen (-)
def register_dashboard_in_lovelace(filename: str, title: str) -> str:
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
    base_key = re.sub(r"-+", "-", base_key)
    base_key = re.sub(r"^-+|-+$", "", base_key)

    if "-" not in base_key:
        base_key = f"dash-{base_key}"

    if not base_key or base_key in ["dashboard", "dashboards", "-"]:
        base_key = "dash-board"

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
        backup_configuration_yaml()
        with open(config_yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                config,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=1000,
                indent=2,
            )

        print(f"✅ Dashboard geregistreerd: {dashboard_key} -> {title}")
        print(f"   📁 Bestand: dashboards/{filename}")
        return f"Dashboard '{title}' geregistreerd als '{dashboard_key}'"
    except Exception as e:
        return f"Schrijven gefaald: {e}"


# =========================
# Dashboard builders
# =========================
def build_simple_single_page_dashboard(title: str) -> Dict[str, Any]:
    states = safe_get_states()

    cards: List[Dict[str, Any]] = [
        {
            "type": "custom:mushroom-title-card",
            "title": title,
            "subtitle": "{{ now().strftime('%d %B %Y') }}",
        }
    ]

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
        "views": [
            {
                "title": "Overzicht",
                "path": "home",
                "icon": "mdi:view-dashboard",
                "type": "sections",
                "sections": [{"type": "grid", "cards": cards}],
            }
        ],
    }


# ✅ Fix: Area-Based Multi-Page Dashboard
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
            entities_by_area.setdefault(area_id, []).append(state)
        else:
            entities_without_area.append(state)

    views: List[Dict[str, Any]] = []

    # VIEW 1: HOME/OVERVIEW
    home_cards: List[Dict[str, Any]] = []
    home_cards.append(
        {"type": "custom:mushroom-title-card", "title": "Hallo! 👋", "subtitle": "{{ now().strftime('%-d %B %Y') }}"}
    )

    chips: List[Dict[str, Any]] = []
    persons = [e for e in states if (e.get("entity_id", "") or "").startswith("person.")]
    lights = [e for e in states if (e.get("entity_id", "") or "").startswith("light.")]

    if persons:
        chips.append({"type": "entity", "entity": persons[0]["entity_id"], "use_entity_picture": True})
    if lights:
        light_count = len([l for l in lights if l.get("state") == "on"])
        chips.append({"type": "template", "icon": "mdi:lightbulb-group", "content": f"{light_count} aan", "tap_action": {"action": "none"}})

    power_sensors = [
        e for e in states if "power" in (e.get("entity_id", "") or "").lower() and (e.get("entity_id", "") or "").startswith("sensor.")
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
        lname = area_name.lower()
        if "woonkamer" in lname or "living" in lname:
            icon = "mdi:sofa"
        elif "slaapkamer" in lname or "bedroom" in lname:
            icon = "mdi:bed"
        elif "keuken" in lname or "kitchen" in lname:
            icon = "mdi:chef-hat"
        elif "badkamer" in lname or "bathroom" in lname:
            icon = "mdi:shower"
        elif "zolder" in lname or "attic" in lname:
            icon = "mdi:home-roof"
        elif "kantoor" in lname or "office" in lname:
            icon = "mdi:desk"
        elif "tuin" in lname or "garden" in lname:
            icon = "mdi:flower"

        temp_info = ""
        if area_temp:
            temp_info = f"{{{{ states('{area_temp[0]['entity_id']}') }}}}°C"
        elif area_climate:
            temp_info = f"{{{{ state_attr('{area_climate[0]['entity_id']}', 'current_temperature') }}}}°C"

        light_info = ""
        if area_lights:
            on_count = len([l for l in area_lights if l.get("state") == "on"])
            light_info = f"{on_count}/{len(area_lights)} lampen"

        secondary_text = " | ".join(filter(None, [temp_info, light_info]))

        home_cards.append(
            {
                "type": "custom:mushroom-template-card",
                "primary": area_name,
                "secondary": secondary_text or "Klik voor details",
                "icon": icon,
                "icon_color": "blue",
                "tap_action": {"action": "navigate", "navigation_path": f"#{sanitize_filename(area_name).replace('_','-')}"},
                "card_mod": {"style": "ha-card { background: rgba(var(--rgb-primary-color), 0.05); }"},
            }
        )

    if entities_without_area:
        home_cards.append({"type": "custom:mushroom-title-card", "title": "Overig"})
        for entity in entities_without_area[:6]:
            entity_id = entity.get("entity_id", "")
            if entity_id.startswith("light."):
                home_cards.append({"type": "custom:mushroom-light-card", "entity": entity_id, "use_light_color": True})
            elif entity_id.startswith("switch."):
                home_cards.append({"type": "custom:mushroom-entity-card", "entity": entity_id, "tap_action": {"action": "toggle"}})

    views.append(
        {
            "title": "Home",
            "path": "home",
            "icon": "mdi:home",
            "type": "sections",
            "sections": [{"type": "grid", "cards": home_cards, "column_span": 1}],
        }
    )

    # VIEW 2+: AREA PAGES
    for area_id, area_entities in sorted(entities_by_area.items()):
        area_name = area_names.get(area_id, area_id)
        area_path = sanitize_filename(area_name).replace("_", "-")

        area_cards: List[Dict[str, Any]] = [
            {"type": "custom:mushroom-title-card", "title": area_name, "subtitle": "{{ now().strftime('%H:%M') }}"}
        ]

        area_lights = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("light.")]
        area_switches = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("switch.")]
        area_climate = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("climate.")]
        area_covers = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("cover.")]
        area_sensors = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("sensor.")]
        area_media = [e for e in area_entities if (e.get("entity_id", "") or "").startswith("media_player.")]

        if area_lights:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "💡 Verlichting"})
            for light in area_lights:
                area_cards.append(
                    {
                        "type": "custom:mushroom-light-card",
                        "entity": light["entity_id"],
                        "use_light_color": True,
                        "show_brightness_control": True,
                        "show_color_control": True,
                        "collapsible_controls": True,
                    }
                )

        if area_climate:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🌡️ Klimaat"})
            for c in area_climate:
                area_cards.append(
                    {
                        "type": "custom:mushroom-climate-card",
                        "entity": c["entity_id"],
                        "show_temperature_control": True,
                        "collapsible_controls": True,
                    }
                )

        if area_covers:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🪟 Raamdecoratie"})
            for cover in area_covers:
                area_cards.append(
                    {
                        "type": "custom:mushroom-cover-card",
                        "entity": cover["entity_id"],
                        "show_buttons_control": True,
                        "show_position_control": True,
                        "collapsible_controls": True,
                    }
                )

        if area_media:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🎵 Media"})
            for m in area_media:
                area_cards.append(
                    {
                        "type": "custom:mushroom-media-player-card",
                        "entity": m["entity_id"],
                        "use_media_info": True,
                        "show_volume_level": True,
                        "collapsible_controls": True,
                    }
                )

        if area_switches:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "🔌 Apparaten"})
            for sw in area_switches:
                area_cards.append({"type": "custom:mushroom-entity-card", "entity": sw["entity_id"], "tap_action": {"action": "toggle"}})

        temp_sensors = [s for s in area_sensors if "temperature" in (s.get("entity_id", "") or "").lower()]
        humidity_sensors = [s for s in area_sensors if "humidity" in (s.get("entity_id", "") or "").lower()]
        if temp_sensors or humidity_sensors:
            area_cards.append({"type": "custom:mushroom-title-card", "title": "📊 Metingen"})
            for t in temp_sensors[:3]:
                area_cards.append({"type": "custom:mushroom-entity-card", "entity": t["entity_id"], "icon": "mdi:thermometer"})
            for h in humidity_sensors[:3]:
                area_cards.append({"type": "custom:mushroom-entity-card", "entity": h["entity_id"], "icon": "mdi:water-percent"})

        if len(area_cards) == 1:
            area_cards.append(
                {
                    "type": "markdown",
                    "content": f"# {area_name}\n\n✅ Nog geen devices toegevoegd aan deze ruimte.\n\nVoeg devices toe via Instellingen → Apparaten & Diensten.",
                }
            )

        views.append(
            {
                "title": area_name,
                "path": area_path,
                "icon": "mdi:door",
                "type": "sections",
                "sections": [{"type": "grid", "cards": area_cards, "column_span": 1}],
            }
        )

    return {"title": title, "views": views}


def build_comprehensive_demo_dashboard(dashboard_title: str) -> Dict[str, Any]:
    dashboard = build_area_based_dashboard(dashboard_title)

    if len(dashboard.get("views", [])) == 1:
        demo_views = [
            {
                "title": "Woonkamer",
                "path": "woonkamer",
                "icon": "mdi:sofa",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "custom:mushroom-title-card", "title": "🛋️ Woonkamer", "subtitle": "Demo ruimte"},
                            {"type": "markdown", "content": "# Woonkamer\n\n✨ Dit is een voorbeeld.\n\nVoeg je eigen devices toe!"},
                        ],
                    }
                ],
            },
            {
                "title": "Slaapkamer",
                "path": "slaapkamer",
                "icon": "mdi:bed",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "custom:mushroom-title-card", "title": "🛏️ Slaapkamer", "subtitle": "Demo ruimte"},
                            {"type": "markdown", "content": "# Slaapkamer\n\n✨ Dit is een voorbeeld.\n\nVoeg je eigen devices toe!"},
                        ],
                    }
                ],
            },
            {
                "title": "Zolder",
                "path": "zolder",
                "icon": "mdi:home-roof",
                "type": "sections",
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "custom:mushroom-title-card", "title": "🏠 Zolder", "subtitle": "Demo ruimte"},
                            {"type": "markdown", "content": "# Zolder\n\n✨ Dit is een voorbeeld.\n\nVoeg je eigen devices toe!"},
                        ],
                    }
                ],
            },
        ]
        dashboard["views"].extend(demo_views)

    return dashboard


# Production default: area-based
def build_dashboard_yaml(dashboard_title: str) -> Dict[str, Any]:
    return build_area_based_dashboard(dashboard_title)


# =========================
# Validation / Debug endpoints
# =========================
def validate_configuration_yaml() -> Tuple[bool, str, Dict[str, Any]]:
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    result: Dict[str, Any] = {
        "exists": False,
        "readable": False,
        "valid_yaml": False,
        "has_lovelace": False,
        "lovelace_mode": None,
        "dashboard_count": 0,
        "errors": [],
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


# =========================
# API: config/status
# =========================
@app.route("/api/config", methods=["GET"])
def api_config():
    ok, msg = conn.probe(force=True)

    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    response_data: Dict[str, Any] = {
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
                "response_preview": (attempt.get("response_text", "") or "")[:200],
            }
            for attempt in conn.probe_attempts
        ]

    return jsonify(response_data)


@app.route("/api/debug/connection", methods=["GET"])
def api_debug_connection():
    ok, msg = conn.probe(force=True)

    debug_data = {
        "connection_ok": ok,
        "connection_message": msg,
        "active_connection": {
            "url": conn.active_base_url,
            "mode": conn.active_mode,
            "has_token": bool(conn.active_token),
            "token_length": len(conn.active_token) if conn.active_token else 0,
        },
        "token_discovery": conn.token_debug,
        "probe_attempts": conn.probe_attempts,
        "environment": {
            "SUPERVISOR_TOKEN": bool(os.environ.get("SUPERVISOR_TOKEN")),
            "HOMEASSISTANT_TOKEN": bool(os.environ.get("HOMEASSISTANT_TOKEN")),
            "HA_CONFIG_PATH": HA_CONFIG_PATH,
            "ADDON_OPTIONS_PATH": ADDON_OPTIONS_PATH,
        },
        "options_json": _read_options_json(),
        "urls_tried": HA_URLS,
    }

    return jsonify(debug_data), 200


@app.route("/api/debug/dashboards", methods=["GET"])
def api_debug_dashboards():
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    debug_info: Dict[str, Any] = {
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
    valid, msg, details = validate_configuration_yaml()
    config_yaml_path = os.path.join(HA_CONFIG_PATH, "configuration.yaml")

    preview: Optional[str] = None
    if os.path.exists(config_yaml_path):
        try:
            with open(config_yaml_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            preview = "".join(lines[:50])
            if len(lines) > 50:
                preview += f"\n... ({len(lines) - 50} more lines)"
        except Exception as e:
            preview = f"Error reading: {e}"

    return jsonify({"valid": valid, "message": msg, "details": details, "path": config_yaml_path, "preview": preview}), 200


# =========================
# API: init lovelace
# =========================
@app.route("/api/init_lovelace", methods=["POST"])
def api_init_lovelace():
    try:
        ok, msg = ensure_lovelace_config()
        if ok:
            ha_call_service("homeassistant", "reload_core_config", {})
            return jsonify({"success": True, "message": msg, "config_path": os.path.join(HA_CONFIG_PATH, "configuration.yaml")}), 200
        return jsonify({"success": False, "error": msg}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# =========================
# API: setup (YAML mode safe)
# =========================
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
        time.sleep(2)
        steps.append("✅ Configuratie herladen (YAML mode)")

        return jsonify({"ok": True, "steps": steps}), 200
    except Exception as e:
        err = str(e)
        print(f"❌ Setup error: {err}")
        return jsonify({"ok": False, "error": err, "steps": steps}), 500


# =========================
# API: create demo dashboard
# =========================
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
        time.sleep(2)
    except Exception as e:
        print(f"⚠️ Reload warning: {e}")

    return jsonify(
        {
            "success": True,
            "filename": fn,
            "register": reg_msg,
            "message": "Dashboard aangemaakt. Ververs je browser (F5) als het niet meteen verschijnt.",
        }
    ), 200


# =========================
# API: create dashboards (supports type)
# =========================
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
    except Exception as e:
        print(f"⚠️ Reload warning: {e}")

    return jsonify(
        {
            "success": True,
            "filename": fn,
            "title": base_title,
            "type": dashboard_type,
            "register": reg_msg,
            "message": f"Dashboard '{base_title}' aangemaakt! ({len(dash.get('views', []))} pagina's)",
        }
    ), 200


# =========================
# API: reload (YAML safe)
# =========================
@app.route("/api/reload_lovelace", methods=["POST"])
def api_reload_lovelace():
    try:
        ha_call_service("homeassistant", "reload_core_config", {})
        return jsonify({"ok": True, "message": "Config herladen"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# HTML UI
# =========================
HTML_PAGE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Dashboard Maker</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  </style>
</head>
<body class="bg-slate-50 text-slate-900">
  <div class="max-w-3xl mx-auto p-5">
    <div class="bg-white rounded-2xl shadow p-5">
      <div class="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div class="text-xl font-bold">Dashboard Maker</div>
          <div class="text-xs text-slate-500">YAML mode • Mushroom • Multi-page dashboards</div>
        </div>
        <div class="flex gap-2 flex-wrap">
          <button onclick="init()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔄 Vernieuwen</button>
          <button onclick="openConnectionDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔍 Verbinding Test</button>
          <button onclick="openDashboardDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">🔍 Dashboard Check</button>
        </div>
      </div>

      <div class="mt-4 flex items-center gap-3">
        <div id="statusDot" class="w-3 h-3 rounded-full bg-yellow-400"></div>
        <div id="statusText" class="text-sm font-semibold">Verbinden…</div>
      </div>

      <div class="mt-4 grid grid-cols-1 md:grid-cols-3 gap-3">
        <div class="border rounded-xl p-3">
          <div class="font-semibold text-sm">1) Verbinding</div>
          <div id="chkEngine" class="text-xs mt-1 text-slate-600">…</div>
        </div>
        <div class="border rounded-xl p-3">
          <div class="font-semibold text-sm">2) Mushroom</div>
          <div id="chkCards" class="text-xs mt-1 text-slate-600">…</div>
        </div>
        <div class="border rounded-xl p-3">
          <div class="font-semibold text-sm">3) Theme</div>
          <div id="chkStyle" class="text-xs mt-1 text-slate-600">…</div>
        </div>
      </div>

      <div class="mt-5 border-t pt-5">
        <div class="text-base font-bold">Stap A — Setup</div>
        <div class="text-xs text-slate-500 mt-1">
          Installeert Mushroom (indien nodig) + theme. In YAML mode moet je resources soms handmatig toevoegen.
        </div>

        <div class="mt-3 flex gap-2 flex-wrap">
          <button onclick="runSetup()" class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-xl font-semibold">⚡ Setup uitvoeren</button>
          <button onclick="initLovelace()" class="bg-slate-100 hover:bg-slate-200 text-slate-900 px-4 py-2 rounded-xl font-semibold">🔧 Lovelace init</button>
        </div>

        <!-- Quick Copy sectie -->
        <div class="mt-4 bg-blue-50 border border-blue-200 rounded-xl p-4">
          <details class="cursor-pointer">
            <summary class="font-semibold text-blue-900 hover:text-blue-700">
              📋 Handmatige Mushroom Setup (kopieer & plak)
            </summary>
            <div class="mt-3 space-y-3">
              <p class="text-sm text-gray-700">Voeg dit toe aan <code class="bg-white px-2 py-1 rounded">/config/configuration.yaml</code>:</p>

              <div class="relative">
                <pre class="bg-gray-900 text-green-400 p-4 rounded-lg overflow-x-auto text-xs font-mono" id="resourcesCodeBlock">lovelace:
  mode: yaml
  resources:
    - url: /local/community/lovelace-mushroom/dist/mushroom.js
      type: module
  dashboards: {}</pre>
                <button onclick="copyResourcesCodeFromBlock()" class="absolute top-2 right-2 bg-blue-500 hover:bg-blue-600 text-white px-3 py-1 rounded text-xs font-semibold">
                  📋 Kopieer
                </button>
              </div>

              <div class="text-xs text-gray-600 bg-yellow-50 border border-yellow-200 p-3 rounded-lg">
                <strong>⚠️ Daarna:</strong><br>
                • Ga naar <strong>Ontwikkelaarstools</strong> → <strong>YAML</strong> → <strong>"ALLE YAML-CONFIGURATIE HERLADEN"</strong><br>
                • Of herstart Home Assistant
              </div>
            </div>
          </details>
        </div>
      </div>

      <div class="mt-5 border-t pt-5">
        <div class="text-base font-bold">Stap B — Maak een dashboard</div>

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

        <div class="mt-3 flex gap-2 flex-wrap">
          <button onclick="createDemo()" class="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-xl font-semibold">🎨 Demo dashboard</button>
          <button onclick="createMine()" class="bg-slate-900 hover:bg-slate-950 text-white px-4 py-2 rounded-xl font-semibold">➕ Maak mijn dashboard</button>
        </div>

        <div class="mt-3 text-xs text-slate-500">
          Tip: na het aanmaken: wacht even en druk op <b>F5</b> om de sidebar te verversen.
        </div>
      </div>

    </div>

    <div class="text-center text-xs text-slate-500 mt-4">
      Dashboard Maker • <span id="ver"></span>
    </div>
  </div>

<script>
  var API_BASE = '';

  function setStatus(text, color) {
    document.getElementById('statusText').textContent = text;
    var dot = document.getElementById('statusDot');
    dot.className = 'w-3 h-3 rounded-full ' + (color === 'green' ? 'bg-green-500' : (color === 'red' ? 'bg-red-500' : 'bg-yellow-400'));
  }

  function setCheck(id, ok, msg) {
    var el = document.getElementById(id);
    el.textContent = (ok ? '✅ ' : '❌ ') + msg;
    el.className = 'text-xs mt-1 ' + (ok ? 'text-emerald-700' : 'text-red-700');
  }

  async function fetchJsonSafe(url, opts) {
    var res = await fetch(url, opts || {});
    var txt = await res.text();
    try {
      return { ok: res.ok, status: res.status, data: JSON.parse(txt), raw: txt };
    } catch (e) {
      return { ok: false, status: res.status, parse_error: e.message, raw: txt };
    }
  }

  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      var cfgRes = await fetch(API_BASE + '/api/config');
      var cfgTxt = await cfgRes.text();
      var cfg;
      try { cfg = JSON.parse(cfgTxt); } catch(e) {
        setStatus('Verbinding mislukt', 'red');
        setCheck('chkEngine', false, 'Non-JSON response van add-on');
        setCheck('chkCards', false, 'Kan niet verbinden');
        setCheck('chkStyle', false, 'Kan niet verbinden');
        console.error('Non-JSON /api/config:', cfgTxt);
        return;
      }

      document.getElementById('ver').textContent = (cfg.app_name || '') + ' v' + (cfg.app_version || '');

      if (cfg.ha_ok) {
        setStatus('Verbonden (' + (cfg.active_mode || 'ok') + ')', 'green');
        setCheck('chkEngine', true, 'OK');
      } else {
        setStatus('Geen verbinding', 'red');
        var errorMsg = cfg.ha_message || 'Geen verbinding';
        if (errorMsg.length > 100) errorMsg = errorMsg.substring(0,100) + '...';
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

  document.getElementById('dashboardType').addEventListener('change', function(e) {
    var help = document.getElementById('dashboardTypeHelp');
    var type = e.target.value;
    if (type === 'area_based') help.textContent = 'Multi-page dashboard met Home overzicht + per ruimte details';
    else if (type === 'simple') help.textContent = 'Alles op één pagina, perfect voor beginners';
  });

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
      msg += '💡 Zie je het niet? Gebruik "Dashboard Check" knop.';

      alert(msg);
      init();
    } catch (e) {
      console.error(e);
      setStatus('Demo mislukt', 'red');
      alert('❌ Demo mislukt.');
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

      var dashboardType = document.getElementById('dashboardType') ? document.getElementById('dashboardType').value : 'area_based';

      var res = await fetch(API_BASE + '/api/create_dashboards', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          base_title: base_title,
          dashboard_type: dashboardType
        })
      });

      var data = await res.json();
      if (!res.ok || !data.success) {
        alert('❌ Maken mislukt: ' + (data.error || 'Onbekend'));
        setStatus('Maken mislukt', 'red');
        return;
      }

      setStatus('Dashboard gereed!', 'green');

      var msg = '✅ Dashboard aangemaakt!\\n\\n';
      msg += '📁 ' + data.title + '\\n';
      msg += '📄 Type: ' + data.type + '\\n';
      msg += '🧾 Bestand: ' + data.filename + '\\n\\n';
      msg += '🔄 Ververs je browser (F5) en check de sidebar!\\n';

      alert(msg);
      init();
    } catch (e) {
      console.error(e);
      setStatus('Maken mislukt', 'red');
      alert('❌ Maken mislukt.');
    }
  }

  async function initLovelace() {
    if (!confirm('Dit voegt de lovelace configuratie toe aan configuration.yaml.\\n\\nEr wordt automatisch een backup gemaakt.\\n\\nDoorgaan?')) return;
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
      init();
    } catch (e) {
      console.error(e);
      setStatus('Initialisatie mislukt', 'red');
      alert('❌ Initialisatie mislukt.');
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

      showSetupResult(r.data.steps);
      setStatus('Setup klaar', 'green');
      init();
    } catch (e) {
      console.error(e);
      alert('❌ Setup error: ' + e.message);
      setStatus('Setup error', 'red');
    }
  }

  function showSetupResult(steps) {
    var resourcesCode = `lovelace:
  mode: yaml
  resources:
    - url: /local/community/lovelace-mushroom/dist/mushroom.js
      type: module
  dashboards: {}`;

    var html = '<div style="max-width: 600px;">';
    html += '<h3 style="font-weight: bold; margin-bottom: 10px;">✅ Setup compleet!</h3>';

    if (steps && steps.length > 0) {
      html += '<div style="margin-bottom: 15px;">';
      steps.forEach(function(step) { html += '<div style="margin: 5px 0;">• ' + step + '</div>'; });
      html += '</div>';
    }

    html += '<h4 style="font-weight: bold; margin: 15px 0 10px 0;">📝 Handmatige stap:</h4>';
    html += '<p style="margin-bottom: 10px;">Voeg dit toe aan configuration.yaml:</p>';

    html += '<div style="position: relative;">';
    html += '<pre style="background: #1e293b; color: #10b981; padding: 15px; border-radius: 8px; overflow-x: auto; font-size: 13px; font-family: monospace; margin: 0;">' + resourcesCode + '</pre>';
    html += '<button onclick="copyResourcesCode()" style="position: absolute; top: 10px; right: 10px; background: #3b82f6; color: white; padding: 5px 10px; border: none; border-radius: 5px; cursor: pointer; font-size: 12px;">📋 Kopieer</button>';
    html += '</div>';

    html += '<div style="margin-top: 15px; padding: 10px; background: #fef3c7; border-left: 4px solid #f59e0b; border-radius: 5px;">';
    html += '<strong>⚠️ Belangrijk:</strong><br>';
    html += '1. Plak bovenstaande code in <code>/config/configuration.yaml</code><br>';
    html += '2. Ga naar Ontwikkelaarstools → YAML → "ALLE YAML-CONFIGURATIE HERLADEN"<br>';
    html += '3. Of herstart Home Assistant<br>';
    html += '4. Maak daarna je dashboard aan';
    html += '</div>';

    html += '</div>';

    var modal = document.createElement('div');
    modal.innerHTML = '<div style="position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 9999; display: flex; align-items: center; justify-content: center;" onclick="this.remove()"><div style="background: white; padding: 30px; border-radius: 15px; max-width: 90%; max-height: 90%; overflow-y: auto;" onclick="event.stopPropagation()">' + html + '<button onclick="this.closest(\\'div[style*=fixed]\\').remove()" style="margin-top: 20px; background: #4f46e5; color: white; padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-weight: bold;">Sluiten</button></div></div>';
    document.body.appendChild(modal);
  }

  window.copyResourcesCode = function() {
    var code = `lovelace:
  mode: yaml
  resources:
    - url: /local/community/lovelace-mushroom/dist/mushroom.js
      type: module
  dashboards: {}`;
    navigator.clipboard.writeText(code).then(function() {
      alert('📋 Gekopieerd naar klembord!');
    }).catch(function() {
      var textarea = document.createElement('textarea');
      textarea.value = code;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      alert('📋 Gekopieerd naar klembord!');
    });
  };

  function copyResourcesCodeFromBlock() {
    var code = document.getElementById('resourcesCodeBlock').textContent;
    navigator.clipboard.writeText(code).then(function() {
      alert('📋 Gekopieerd! Plak in /config/configuration.yaml');
    }).catch(function() {
      var textarea = document.createElement('textarea');
      textarea.value = code;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      alert('📋 Gekopieerd! Plak in /config/configuration.yaml');
    });
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

  init();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return Response(HTML_PAGE, mimetype="text/html")


# =========================
# Entrypoint
# =========================
if __name__ == "__main__":
    # For local debugging. In HA add-on, ingress usually handles serving.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8099")))
