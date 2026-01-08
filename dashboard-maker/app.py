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

APP_VERSION = "1.0.9"
APP_NAME = "Dashboard Maker"

app = Flask(__name__)

# -------------------------
# Paths
# -------------------------
HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
DASHBOARDS_PATH = os.environ.get("DASHBOARDS_PATH") or os.path.join(HA_CONFIG_PATH, "dashboards")
ADDON_OPTIONS_PATH = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")

# --- Mushroom install ---
MUSHROOM_VERSION = "3.3.0"
MUSHROOM_GITHUB_ZIP = f"https://github.com/piitaya/lovelace-mushroom/releases/download/v{MUSHROOM_VERSION}/mushroom.zip"
WWW_COMMUNITY = os.path.join(HA_CONFIG_PATH, "www", "community")
MUSHROOM_PATH = os.path.join(WWW_COMMUNITY, "mushroom")

# --- Themes ---
THEMES_PATH = os.path.join(HA_CONFIG_PATH, "themes")
DASHBOARD_THEME_FILE = os.path.join(THEMES_PATH, "dashboard_maker.yaml")
THEME_NAME = "Dashboard Maker"

# --- HA URLs ---
HA_URLS = [
    "http://supervisor/core",
    "http://homeassistant:8123",
    "http://localhost:8123",
    "http://127.0.0.1:8123",
]

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
    except Exception as e:
        print(f"Warning: Could not read options.json: {e}")
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
def discover_tokens() -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """
    Returns (user_token, supervisor_token, debug_info)
    """
    debug_info: Dict[str, Any] = {
        "addon_options_exists": os.path.exists(ADDON_OPTIONS_PATH),
        "env_homeassistant_token": bool(os.environ.get("HOMEASSISTANT_TOKEN")),
        "env_supervisor_token": bool(os.environ.get("SUPERVISOR_TOKEN")),
        "supervisor_token_files_checked": [],
    }

    opts = _read_options_json()
    debug_info["options_json_keys"] = sorted(list(opts.keys()))

    user_tok = (opts.get("access_token", "") or "").strip()
    if not user_tok:
        user_tok = (os.environ.get("HOMEASSISTANT_TOKEN", "") or "").strip()

    debug_info["user_token_found"] = bool(user_tok)
    if user_tok:
        debug_info["user_token_length"] = len(user_tok)
        debug_info["user_token_prefix"] = (user_tok[:20] + "...") if len(user_tok) > 20 else user_tok

    sup_tok = (opts.get("supervisor_token", "") or "").strip()
    if not sup_tok:
        sup_tok = (os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()

    if not sup_tok:
        for p in ["/var/run/supervisor_token", "/run/supervisor_token", "/data/supervisor_token"]:
            debug_info["supervisor_token_files_checked"].append(p)
            sup_tok = _read_file(p)
            if sup_tok:
                debug_info["supervisor_token_file_found"] = p
                break

    debug_info["supervisor_token_found"] = bool(sup_tok)

    return (user_tok or None, sup_tok or None, debug_info)

USER_TOKEN, SUPERVISOR_TOKEN, TOKEN_DEBUG = discover_tokens()

print(f"\n{'='*60}")
print(f"{APP_NAME} {APP_VERSION}")
print(f"{'='*60}")
print(f"Config path: {HA_CONFIG_PATH}")
print(f"Dashboards path: {DASHBOARDS_PATH}")
print(f"Options.json: {ADDON_OPTIONS_PATH}")
print(f"  - Exists: {TOKEN_DEBUG.get('addon_options_exists')}")
print(f"  - Keys: {TOKEN_DEBUG.get('options_json_keys')}")
print(f"\nToken Status:")
print(f"  - User token (LLAT): {'✓ Found' if USER_TOKEN else '✗ NOT FOUND'}")
if USER_TOKEN:
    print(f"    Length: {TOKEN_DEBUG.get('user_token_length')}")
    print(f"    Prefix: {TOKEN_DEBUG.get('user_token_prefix')}")
print(f"  - Supervisor token: {'✓ Found' if SUPERVISOR_TOKEN else '✗ NOT FOUND'}")
print(f"\nWill try these HA URLs in order:")
for url in HA_URLS:
    print(f"  - {url}")
print(f"{'='*60}\n")

# -------------------------
# HA Connection (no globals mutation inside methods)
# -------------------------
class HAConnection:
    def __init__(self):
        self.active_base_url: Optional[str] = None
        self.active_token: Optional[str] = None
        self.active_mode: str = "unknown"
        self.last_probe: Optional[str] = None
        self.probe_attempts: List[Dict[str, Any]] = []

        self.user_token: Optional[str] = None
        self.supervisor_token: Optional[str] = None
        self.token_debug: Dict[str, Any] = {}

        self.refresh_tokens()

    def refresh_tokens(self) -> None:
        u, s, dbg = discover_tokens()
        self.user_token = u
        self.supervisor_token = s
        self.token_debug = dbg

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

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
                timeout=5,
                verify=False
            )

            debug["status_code"] = r.status_code
            debug["response_length"] = len(r.text)

            if r.status_code == 200:
                try:
                    data = r.json()
                    debug["response_message"] = data.get("message", "")
                except Exception:
                    debug["response_text"] = r.text[:200]
                return True, "OK", debug

            if r.status_code == 401:
                debug["error"] = "Unauthorized"
                return False, "Unauthorized (401)", debug
            if r.status_code == 403:
                debug["error"] = "Forbidden"
                return False, "Forbidden (403)", debug

            debug["error"] = f"HTTP {r.status_code}"
            debug["response_text"] = r.text[:200]
            return False, f"HTTP {r.status_code}", debug

        except requests.exceptions.Timeout:
            debug["error"] = "Timeout"
            return False, "Timeout", debug
        except requests.exceptions.ConnectionError as e:
            debug["error"] = f"Connection error: {str(e)[:100]}"
            return False, "Connection refused", debug
        except Exception as e:
            debug["error"] = f"Exception: {str(e)[:100]}"
            return False, str(e)[:100], debug

    def probe(self, force: bool = False) -> Tuple[bool, str]:
        if self.active_base_url and self.active_token and not force:
            return True, f"cached:{self.active_mode}"

        self.refresh_tokens()
        self.probe_attempts = []

        attempts: List[Tuple[str, str, str]] = []

        # Prefer LLAT everywhere
        if self.user_token:
            for url in HA_URLS:
                mode = "supervisor" if "supervisor" in url else "direct"
                attempts.append((url, self.user_token, mode))

        # Supervisor token only makes sense via supervisor/core typically
        if self.supervisor_token:
            for url in HA_URLS:
                if "supervisor" in url:
                    attempts.append((url, self.supervisor_token, "supervisor"))

        if not attempts:
            msg = (
                "❌ Geen tokens gevonden! Voeg 'access_token' toe in add-on opties.\n"
                f"Debug: {self.token_debug}"
            )
            self.last_probe = msg
            return False, msg

        print(f"\n🔍 Probing {len(attempts)} connection attempts...")
        for url, token, mode in attempts:
            success, message, debug = self._test_connection(url, token, mode)
            self.probe_attempts.append(debug)
            print(f"  {'✓' if success else '✗'} {mode:12} {url:35} → {message}")

            if success:
                self.active_base_url = url
                self.active_token = token
                self.active_mode = mode
                self.last_probe = "ok"
                print(f"  ✅ Using: {mode} via {url}\n")
                return True, f"OK via {mode} ({url})"

        error_msg = "❌ Alle verbindingen gefaald!\n\nGeprobeerd:\n"
        for att in self.probe_attempts:
            error_msg += f"  • {att['mode']} {att['url']}: {att.get('error', 'unknown')}\n"
        error_msg += "\n💡 Oplossing:\n"
        error_msg += "- Maak een Long-Lived Access Token in Home Assistant (profiel)\n"
        error_msg += "- Zet hem in de add-on opties als: access_token\n"

        self.last_probe = error_msg
        print(error_msg)
        return False, error_msg

    def request(self, method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response:
        ok, _ = self.probe(force=False)
        if not ok or not self.active_base_url:
            ok2, msg2 = self.probe(force=True)
            if not ok2 or not self.active_base_url:
                raise requests.exceptions.ConnectionError(msg2)

        if not path.startswith("/"):
            path = "/" + path

        url = f"{self.active_base_url}{path}"
        r = requests.request(
            method,
            url,
            headers=self._headers(self.active_token),
            json=json_body,
            timeout=timeout,
            verify=False
        )

        if r.status_code in (401, 403):
            # reset and try once again
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

# -------------------------
# Mushroom install + resources
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
        return "Mushroom resource (best effort) OK"
    except Exception:
        return "Mushroom resource (best effort) OK"

# -------------------------
# Theme generator
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

  # Let op: card-mod moet je eventueel nog apart installeren (optioneel).
  # Zonder card-mod werkt dit theme nog steeds prima.
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
    r, st = ha_call_service("frontend", "set_theme", {"name": THEME_NAME, "mode": "auto"})
    if st == 200 and r.get("ok"):
        return "Theme actief (auto licht/donker)"
    r2, st2 = ha_call_service("frontend", "set_theme", {"name": THEME_NAME})
    if st2 == 200 and r2.get("ok"):
        return "Theme actief (fallback)"
    return "Theme geïnstalleerd (activeren niet gelukt)"

# -------------------------
# Minimal dashboard builder (safe test)
# -------------------------
def build_dashboard_yaml(dashboard_title: str) -> Dict[str, Any]:
    return {
        "title": dashboard_title,
        "views": [
            {
                "title": "WOW",
                "path": "wow",
                "icon": "mdi:star-four-points",
                "cards": [
                    {"type": "markdown", "content": f"# {dashboard_title}\n\n✅ Dashboard Maker werkt!\n\n- Mushroom: {mushroom_installed()}\n- Tijd: {{% raw %}}{{{{ now() }}}}{{% endraw %}}"}
                ],
            }
        ],
    }

# -------------------------
# HTML Wizard (complete)
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
            <button onclick="reloadDashboards()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🔄 Vernieuwen
            </button>
            <button onclick="openDebug()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">
              🧾 Debug
            </button>
          </div>
        </div>
      </div>

      <!-- Progress -->
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

      <!-- Step 1 -->
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
          <div class="text-sm text-slate-500 flex items-center">
            <span id="setupHint">Klik één keer. Wij doen de rest.</span>
          </div>
        </div>
      </div>

      <!-- Step 2 -->
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
          <div class="text-sm text-slate-500 flex items-center">
            <span>Je kunt dit later verwijderen.</span>
          </div>
        </div>
      </div>

      <!-- Step 3 -->
      <div id="step3" class="border border-slate-200 rounded-2xl p-5 mt-4 opacity-50 pointer-events-none">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-xl font-bold text-slate-900">Stap 3 — Maak jouw dashboards</h2>
            <p class="text-slate-600 mt-1">Geef een naam. Wij maken 2 dashboards: <b>Simpel</b> & <b>Uitgebreid</b>.</p>
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
            <button onclick="toggleAdvanced()" class="w-full sm:w-auto bg-white border border-gray-300 text-gray-800 py-3 px-4 rounded-xl text-lg font-semibold hover:bg-gray-100 shadow-lg">
              🔧 Bekijk techniek (optioneel)
            </button>
          </div>
        </div>

        <div id="advancedPanel" class="hidden mt-4 bg-slate-50 border border-slate-200 rounded-2xl p-4">
          <div class="flex items-center justify-between">
            <div class="font-bold text-slate-900">Technische output</div>
            <button onclick="copyAll()" class="text-sm bg-white border border-gray-300 px-3 py-1 rounded-lg hover:bg-gray-100">📋 Copy</button>
          </div>
          <p class="text-xs text-slate-500 mt-1">Niet nodig om te gebruiken.</p>
          <div class="bg-gray-900 text-green-400 p-3 rounded-xl overflow-x-auto text-xs font-mono mt-3" style="min-height: 120px;">
            <pre id="advancedOut">—</pre>
          </div>
        </div>
      </div>

      <!-- Step 4 -->
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
  const API_BASE = window.location.pathname.replace(/\/$/, '');

  function setStatus(text, color = 'gray') {
    document.getElementById('status').innerHTML =
      '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
      '<span class="text-' + color + '-700">' + text + '</span>';
  }

  function setDot(step, active) {
    const el = document.getElementById(step + 'Dot');
    const dot = el.querySelector('span');
    if (active) {
      el.classList.remove('text-slate-500');
      dot.className = 'w-3 h-3 rounded-full bg-indigo-500 inline-block';
    } else {
      el.classList.add('text-slate-500');
      dot.className = 'w-3 h-3 rounded-full bg-slate-300 inline-block';
    }
  }

  function unlockStep(stepId) {
    const el = document.getElementById(stepId);
    el.classList.remove('opacity-50', 'pointer-events-none');
  }

  function showStep4() {
    document.getElementById('step4').classList.remove('hidden');
    setDot('step4', true);
  }

  function escapeHtml(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function setCheck(id, ok, msg) {
    const el = document.getElementById(id);
    el.textContent = (ok ? '✅ ' : '❌ ') + msg;
    el.className = 'text-sm mt-1 ' + (ok ? 'text-green-700' : 'text-red-700');
  }

  async function init() {
    setStatus('Verbinden…', 'yellow');
    try {
      const cfgRes = await fetch(API_BASE + '/api/config');
      const cfg = await cfgRes.json();

      if (cfg.ha_ok) {
        setStatus('Verbonden (' + (cfg.active_mode || 'ok') + ')', 'green');
        setCheck('chkEngine', true, 'OK');
      } else {
        setStatus('Geen verbinding', 'red');
        setCheck('chkEngine', false, cfg.ha_message || 'Geen verbinding');
      }

      setCheck('chkCards', true, cfg.mushroom_installed ? 'Al geïnstalleerd' : 'Klaar om te installeren');
      setCheck('chkStyle', true, cfg.theme_file_exists ? 'Al aanwezig' : 'Klaar om te installeren');

      setDot('step1', true);
    } catch (e) {
      console.error(e);
      setStatus('Verbinding mislukt', 'red');
      setCheck('chkEngine', false, 'Kan niet verbinden');
      setCheck('chkCards', false, 'Kan niet verbinden');
      setCheck('chkStyle', false, 'Kan niet verbinden');
    }
  }

  async function runSetup() {
    const preset = document.getElementById('preset').value;
    const density = document.getElementById('density').value;

    document.getElementById('setupHint').textContent = 'Bezig… (Mushroom + theme + auto licht/donker)';
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
        return alert('❌ Instellen mislukt: ' + (data.error || 'Onbekend'));
      }

      setCheck('chkCards', true, 'Klaar');
      setCheck('chkStyle', true, 'Klaar');
      document.getElementById('setupHint').textContent = 'Klaar! Je kunt verder.';

      unlockStep('step2');
      unlockStep('step3');
      setDot('step2', true);
      setDot('step3', true);

      alert('✅ Setup klaar!\n\n' + (data.steps ? data.steps.join('\n') : ''));
    } catch (e) {
      console.error(e);
      document.getElementById('setupHint').textContent = 'Dit lukte niet. Probeer opnieuw.';
      alert('❌ Instellen mislukt.');
    }
  }

  async function createDemo() {
    try {
      const res = await fetch(API_BASE + '/api/create_demo', { method: 'POST' });
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

    try {
      const res = await fetch(API_BASE + '/api/create_dashboards', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ base_title })
      });
      const data = await res.json();
      if (!res.ok || !data.success) return alert('❌ Maken mislukt: ' + (data.error || 'Onbekend'));

      const adv = document.getElementById('advancedPanel');
      if (!adv.classList.contains('hidden')) {
        document.getElementById('advancedOut').textContent = (data.simple_code || '') + '\n---\n' + (data.advanced_code || '');
      }

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

  function toggleAdvanced() {
    document.getElementById('advancedPanel').classList.toggle('hidden');
  }

  function copyAll() {
    const text = document.getElementById('advancedOut').textContent || '';
    navigator.clipboard.writeText(text).then(() => alert('📋 Gekopieerd!'));
  }

  async function loadDashboards() {
    const response = await fetch(API_BASE + '/api/dashboards');
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
    const res = await fetch(API_BASE + '/api/debug/ha');
    const data = await res.json();
    alert(JSON.stringify(data, null, 2));
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

@app.route("/")
def index():
    html = HTML_PAGE.replace("__APP_NAME__", APP_NAME).replace("__APP_VERSION__", APP_VERSION)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# -------------------------
# API
# -------------------------
@app.route("/api/config", methods=["GET"])
def api_config():
    ok, msg = conn.probe(force=False)
    return jsonify({
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "ha_ok": bool(ok),
        "ha_message": msg,
        "active_mode": conn.active_mode,
        "active_base_url": conn.active_base_url,
        "dashboards_path": DASHBOARDS_PATH,
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "mushroom_installed": mushroom_installed(),
        "theme_file_exists": os.path.exists(DASHBOARD_THEME_FILE),
        "options_json_found": os.path.exists(ADDON_OPTIONS_PATH),
        "options_json_path": ADDON_OPTIONS_PATH,
        "token_debug": conn.token_debug,
    })

@app.route("/api/debug/ha", methods=["GET"])
def api_debug_ha():
    info = {
        "active_base_url": conn.active_base_url,
        "active_mode": conn.active_mode,
        "user_token_present": bool(conn.user_token),
        "supervisor_token_present": bool(conn.supervisor_token),
        "options_json_path": ADDON_OPTIONS_PATH,
        "options_json_exists": os.path.exists(ADDON_OPTIONS_PATH),
        "token_debug": conn.token_debug,
        "probe_attempts": conn.probe_attempts,
        "last_probe": conn.last_probe,
    }

    try:
        ok, msg = conn.probe(force=True)
        info["probe_ok"] = ok
        info["probe_message"] = msg

        if ok:
            r = conn.request("GET", "/api/", timeout=10)
            return jsonify({
                "ok": (r.status_code == 200),
                "status": r.status_code,
                "body": r.text[:400],
                "info": info,
            }), 200

        return jsonify({"ok": False, "error": msg, "info": info}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "info": info}), 200

@app.route("/api/setup", methods=["POST"])
def api_setup():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

    data = request.json or {}
    preset = (data.get("preset") or "indigo_luxe").strip()
    density = (data.get("density") or "comfy").strip()

    steps: List[str] = []
    try:
        steps.append(install_mushroom())
        steps.append(ensure_mushroom_resource())
        steps.append(install_dashboard_theme(preset, density))
        steps.append(try_set_theme_auto())

        # reload lovelace best effort
        ha_call_service("lovelace", "reload", {})
        steps.append("Lovelace reload (best effort)")

        return jsonify({"ok": True, "steps": steps}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "steps": steps}), 500

@app.route("/api/create_demo", methods=["POST"])
def api_create_demo():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    title = "WOW Demo Dashboard"
    dash = build_dashboard_yaml(title)
    code = safe_yaml_dump(dash)
    fn = next_available_filename(DASHBOARDS_PATH, f"{sanitize_filename(title)}.yaml")
    write_text_file(os.path.join(DASHBOARDS_PATH, fn), code)
    return jsonify({"success": True, "filename": fn}), 200

@app.route("/api/create_dashboards", methods=["POST"])
def api_create_dashboards():
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    data = request.json or {}
    base_title = (data.get("base_title") or "").strip()
    if not base_title:
        return jsonify({"success": False, "error": "Naam ontbreekt."}), 400

    simple_title = f"{base_title} Simpel"
    adv_title = f"{base_title} Uitgebreid"

    simple_dash = build_dashboard_yaml(simple_title)
    adv_dash = build_dashboard_yaml(adv_title)

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
    ok, msg = conn.probe(force=True)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400

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
