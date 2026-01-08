#!/usr/bin/env python3
from flask import Flask, request, jsonify
import yaml
import os
import re
from pathlib import Path
import requests

app = Flask(__name__)

HA_CONFIG_PATH = os.environ.get("HA_CONFIG_PATH", "/config")
TEMPLATES_PATH = os.environ.get("TEMPLATES_PATH") or os.path.join(HA_CONFIG_PATH, "include", "templates")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

Path(TEMPLATES_PATH).mkdir(parents=True, exist_ok=True)

print(f"Config path: {HA_CONFIG_PATH}")
print(f"Templates path: {TEMPLATES_PATH}")
print(f"Supervisor token available: {bool(SUPERVISOR_TOKEN)}")

def sanitize_filename(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "_", name)
    if not name:
        name = "unnamed"
    return name[:80]

def ha_headers():
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }

def get_ha_entities():
    if not SUPERVISOR_TOKEN:
        print("No supervisor token, returning demo data")
        return [
            {"entity_id": "light.woonkamer", "domain": "light", "name": "Woonkamer Lamp"},
            {"entity_id": "sensor.temperatuur", "domain": "sensor", "name": "Temperatuur"},
        ]

    try:
        resp = requests.get("http://supervisor/core/api/states", headers=ha_headers(), timeout=10)
        if resp.status_code != 200:
            print(f"Failed to fetch entities: {resp.status_code}")
            return []

        states = resp.json()
        entities = []
        for s in states:
            entity_id = s.get("entity_id", "")
            if not entity_id:
                continue
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            friendly = (s.get("attributes") or {}).get("friendly_name", entity_id)
            entities.append({"entity_id": entity_id, "domain": domain, "name": friendly})
        return entities
    except Exception as e:
        print(f"Error getting entities: {e}")
        return []

@app.route("/")
def index():
    html_content = """<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Template Maker Pro</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-br from-purple-50 to-blue-100 min-h-screen p-4">
  <div class="max-w-5xl mx-auto">
    <div class="bg-white rounded-2xl shadow-2xl p-8 mb-6">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-4xl font-bold text-purple-800">🎨 Template Maker Pro</h1>
          <p class="text-gray-600 mt-2">Maak templates zonder YAML-trauma! Zelfs je oma snapt dit!</p>
        </div>
        <div id="status" class="text-sm">
          <span class="inline-block w-3 h-3 bg-gray-400 rounded-full mr-2 animate-pulse"></span>
          <span>Verbinding maken...</span>
        </div>
      </div>

      <div id="tokenWarning" class="hidden mb-6 bg-yellow-50 border-l-4 border-yellow-400 p-4 rounded">
        <div class="flex">
          <div class="flex-shrink-0">⚠️</div>
          <div class="ml-3">
            <p class="text-sm text-yellow-700">
              <strong>Supervisor Token ontbreekt!</strong><br>
              Ga naar de addon configuratie en vul je Long-Lived Access Token in.
            </p>
          </div>
        </div>
      </div>

      <div class="mb-6">
        <label class="block text-lg font-semibold text-gray-700 mb-2">📝 Naam van je template</label>
        <input type="text" id="templateName" placeholder="bijv. Lampen Teller" 
               class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-purple-500 focus:outline-none">
      </div>

      <div class="mb-6 bg-purple-50 p-6 rounded-xl">
        <label class="block text-lg font-semibold text-gray-700 mb-3">🎯 Wat wil je maken?</label>
        <select id="templateType" onchange="showTemplateOptions()" 
                class="w-full px-4 py-3 text-lg border-2 border-gray-300 rounded-xl focus:border-purple-500 focus:outline-none">
          <option value="">-- Kies een template type --</option>
          <option value="count_lights">💡 Tel hoeveel lampen aan zijn</option>
          <option value="average_temp">🌡️ Gemiddelde temperatuur van sensoren</option>
          <option value="any_open">🚪 Check of er iets open staat</option>
          <option value="power_total">⚡ Totaal stroomverbruik</option>
        </select>
        <div id="templateOptions" class="mt-4"></div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <button onclick="createTemplate()" 
                class="w-full bg-gradient-to-r from-purple-600 to-blue-600 text-white py-4 px-6 rounded-xl text-lg font-semibold hover:from-purple-700 hover:to-blue-700 transition-all shadow-lg">
          💾 Template Maken!
        </button>
        <button onclick="loadTemplates()" 
                class="w-full bg-gradient-to-r from-gray-600 to-gray-800 text-white py-4 px-6 rounded-xl text-lg font-semibold hover:from-gray-700 hover:to-gray-900 transition-all shadow-lg">
          📋 Mijn Templates
        </button>
      </div>

      <div id="preview" class="hidden mt-6 bg-gray-50 p-6 rounded-xl">
        <h3 class="text-xl font-bold text-gray-800 mb-3">👀 Preview YAML</h3>
        <pre id="previewCode" class="bg-gray-900 text-green-400 p-4 rounded-lg overflow-x-auto text-sm font-mono"></pre>
      </div>
    </div>

    <div id="templatesList" class="bg-white rounded-2xl shadow-2xl p-8 hidden">
      <h2 class="text-2xl font-bold text-gray-800 mb-4">📚 Opgeslagen Templates</h2>
      <div id="templatesContent" class="space-y-3"></div>
    </div>
  </div>

  <script>
    let entities = [];
    const API_BASE = window.location.pathname.replace(/\\/$/, '');

    function setStatus(text, color = 'gray') {
      document.getElementById('status').innerHTML = 
        '<span class="inline-block w-3 h-3 bg-' + color + '-500 rounded-full mr-2"></span>' +
        '<span class="text-' + color + '-700">' + text + '</span>';
    }

    async function init() {
      setStatus('Verbinden...', 'yellow');
      
      try {
        const configRes = await fetch(API_BASE + '/api/config');
        const config = await configRes.json();
        
        if (!config.token_configured) {
          document.getElementById('tokenWarning').classList.remove('hidden');
        }

        const entRes = await fetch(API_BASE + '/api/entities');
        entities = await entRes.json();
        
        setStatus('Verbonden (' + entities.length + ' apparaten)', 'green');
      } catch (error) {
        setStatus('Verbinding mislukt', 'red');
        console.error('Init error:', error);
      }
    }

    function escapeHtml(str) {
      return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function showTemplateOptions() {
      const type = document.getElementById('templateType').value;
      const container = document.getElementById('templateOptions');
      
      if (!type) {
        container.innerHTML = '';
        return;
      }

      let filtered = entities;
      if (type === 'count_lights') {
        container.innerHTML = '<p class="text-sm text-gray-600">💡 Dit telt automatisch alle lampen. Geen selectie nodig!</p>';
        return;
      } else if (type === 'average_temp') {
        filtered = entities.filter(e => e.domain === 'sensor' && e.name.toLowerCase().includes('temp'));
      } else if (type === 'any_open') {
        filtered = entities.filter(e => e.domain === 'binary_sensor');
      } else if (type === 'power_total') {
        filtered = entities.filter(e => e.domain === 'sensor' && (e.name.toLowerCase().includes('power') || e.name.toLowerCase().includes('vermogen')));
      }

      let html = '<label class="block text-sm font-semibold text-gray-700 mb-2">Selecteer apparaten (klik om te selecteren):</label>';
      html += '<div class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-64 overflow-y-auto">';
      
      filtered.forEach(e => {
        html += '<div class="entity-select p-3 border-2 border-gray-200 rounded-lg cursor-pointer hover:bg-purple-50 hover:border-purple-300 transition-all" onclick="toggleEntity(this, \\'' + e.entity_id + '\\')">';
        html += '<div class="font-semibold text-sm">' + escapeHtml(e.name) + '</div>';
        html += '<div class="text-xs text-gray-500">' + escapeHtml(e.entity_id) + '</div>';
        html += '</div>';
      });
      
      html += '</div>';
      container.innerHTML = html;
    }

    let selectedEntities = [];
    
    function toggleEntity(element, entityId) {
      const index = selectedEntities.indexOf(entityId);
      if (index > -1) {
        selectedEntities.splice(index, 1);
        element.classList.remove('bg-purple-100', 'border-purple-500');
        element.classList.add('border-gray-200');
      } else {
        selectedEntities.push(entityId);
        element.classList.add('bg-purple-100', 'border-purple-500');
        element.classList.remove('border-gray-200');
      }
    }

    async function createTemplate() {
      const name = document.getElementById('templateName').value.trim();
      const type = document.getElementById('templateType').value;

      if (!name) {
        alert('❌ Vul een naam in!');
        return;
      }

      if (!type) {
        alert('❌ Kies een template type!');
        return;
      }

      if (type !== 'count_lights' && selectedEntities.length === 0) {
        alert('❌ Selecteer minstens 1 apparaat!');
        return;
      }

      try {
        const response = await fetch(API_BASE + '/api/create_template', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            type: type,
            entities: selectedEntities,
            name: name,
            icon: 'mdi:help'
          })
        });

        const result = await response.json();

        if (response.ok) {
          document.getElementById('preview').classList.remove('hidden');
          document.getElementById('previewCode').textContent = result.code;
          alert('✅ Template opgeslagen als ' + result.filename + '\\n\\n⚠️ Vergeet niet Home Assistant te herladen!');
        } else {
          alert('❌ Fout: ' + (result.error || 'Onbekende fout'));
        }
      } catch (error) {
        alert('❌ Fout: ' + error.message);
      }
    }

    async function loadTemplates() {
      try {
        const response = await fetch(API_BASE + '/api/templates');
        const templates = await response.json();

        const list = document.getElementById('templatesList');
        const content = document.getElementById('templatesContent');

        if (templates.length === 0) {
          list.classList.add('hidden');
          alert('Nog geen templates aangemaakt!');
          return;
        }

        list.classList.remove('hidden');
        
        let html = '';
        templates.forEach(t => {
          html += '<div class="bg-gray-50 border-2 border-gray-200 rounded-lg p-4 flex justify-between items-center">';
          html += '<div><div class="font-semibold">' + escapeHtml(t.name) + '</div>';
          html += '<div class="text-sm text-gray-500">' + escapeHtml(t.filename) + '</div></div>';
          html += '<button onclick="deleteTemplate(\\'' + t.filename + '\\')" class="bg-red-500 text-white px-4 py-2 rounded-lg hover:bg-red-600">🗑️ Verwijder</button>';
          html += '</div>';
        });
        
        content.innerHTML = html;
        list.scrollIntoView({ behavior: 'smooth' });
      } catch (error) {
        alert('Kon templates niet laden: ' + error.message);
      }
    }

    async function deleteTemplate(filename) {
      if (!confirm('Weet je zeker dat je ' + filename + ' wilt verwijderen?')) return;

      try {
        const response = await fetch(API_BASE + '/api/delete_template', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: filename })
        });

        const result = await response.json();

        if (response.ok) {
          alert('✅ Template verwijderd!');
          loadTemplates();
        } else {
          alert('❌ Fout: ' + (result.error || 'Onbekende fout'));
        }
      } catch (error) {
        alert('❌ Fout: ' + error.message);
      }
    }

    init();
  </script>
</body>
</html>"""
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "token_configured": bool(SUPERVISOR_TOKEN),
        "templates_path": TEMPLATES_PATH
    })

@app.route("/api/entities", methods=["GET"])
def api_entities():
    return jsonify(get_ha_entities())

@app.route("/api/templates", methods=["GET"])
def list_templates():
    templates = []
    try:
        if os.path.exists(TEMPLATES_PATH):
            for filename in sorted(os.listdir(TEMPLATES_PATH)):
                if filename.endswith(".yaml"):
                    templates.append({
                        "filename": filename,
                        "name": filename.replace(".yaml", "").replace("_", " ").title()
                    })
    except Exception as e:
        print(f"Error listing templates: {e}")
    return jsonify(templates)

@app.route("/api/create_template", methods=["POST"])
def create_template():
    try:
        data = request.json or {}
        template_type = data.get("type")
        selected_entities = data.get("entities", [])
        name = data.get("name", "Nieuwe Sensor")
        icon = data.get("icon", "mdi:help")

        safe_name = sanitize_filename(name)
        filename = f"{safe_name}.yaml"
        filepath = os.path.join(TEMPLATES_PATH, filename)

        template_config = None

        if template_type == "count_lights":
            template_config = {
                "template": [{
                    "sensor": [{
                        "name": name,
                        "unique_id": f"template_{safe_name}",
                        "state": "{{ states.light | selectattr('state', 'eq', 'on') | list | count }}",
                        "icon": "mdi:lightbulb-group",
                        "unit_of_measurement": "lampen"
                    }]
                }]
            }
        elif template_type == "average_temp" and selected_entities:
            entities_list = '", "'.join(selected_entities)
            state_template = '{{ ["' + entities_list + '"] | map("states") | reject("in", ["unknown", "unavailable"]) | map("float") | average | round(1) }}'
            template_config = {
                "template": [{
                    "sensor": [{
                        "name": name,
                        "unique_id": f"template_{safe_name}",
                        "state": state_template,
                        "icon": "mdi:thermometer",
                        "unit_of_measurement": "°C",
                        "device_class": "temperature"
                    }]
                }]
            }
        elif template_type == "any_open" and selected_entities:
            entities_list = '", "'.join(selected_entities)
            state_template = '{{ ["' + entities_list + '"] | map("states") | select("in", ["on", "open"]) | list | count > 0 }}'
            template_config = {
                "template": [{
                    "binary_sensor": [{
                        "name": name,
                        "unique_id": f"template_{safe_name}",
                        "state": state_template,
                        "device_class": "door"
                    }]
                }]
            }
        elif template_type == "power_total" and selected_entities:
            entities_list = '", "'.join(selected_entities)
            state_template = '{{ ["' + entities_list + '"] | map("states") | reject("in", ["unknown", "unavailable"]) | map("float") | sum | round(2) }}'
            template_config = {
                "template": [{
                    "sensor": [{
                        "name": name,
                        "unique_id": f"template_{safe_name}",
                        "state": state_template,
                        "icon": "mdi:flash",
                        "unit_of_measurement": "W",
                        "device_class": "power"
                    }]
                }]
            }

        if not template_config:
            return jsonify({"error": "Ongeldig template type"}), 400

        with open(filepath, "w") as f:
            yaml.dump(template_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        preview_code = yaml.dump(template_config, default_flow_style=False, allow_unicode=True, sort_keys=False)

        return jsonify({
            "success": True,
            "filename": filename,
            "code": preview_code,
            "message": f"Template opgeslagen als {filename}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/delete_template", methods=["POST"])
def delete_template():
    try:
        data = request.json or {}
        filename = data.get("filename")
        if not filename:
            return jsonify({"error": "Geen filename opgegeven"}), 400

        filepath = os.path.join(TEMPLATES_PATH, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            return jsonify({"success": True, "message": f"{filename} verwijderd"})
        else:
            return jsonify({"error": "Bestand niet gevonden"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("Template Maker Pro Starting...")
    print("=" * 50)
    print(f"Config path: {HA_CONFIG_PATH}")
    print(f"Templates path: {TEMPLATES_PATH}")
    print(f"Supervisor token: {'Available' if SUPERVISOR_TOKEN else 'Missing'}")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=8099, debug=False)
