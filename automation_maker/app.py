#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yaml
import os
import re
from pathlib import Path
import requests

app = Flask(__name__)
CORS(app)

HA_CONFIG_PATH = os.environ.get('HA_CONFIG_PATH', '/config')
AUTOMATIONS_PATH = os.environ.get('AUTOMATIONS_PATH') or os.path.join(HA_CONFIG_PATH, 'include', 'automations')
SUPERVISOR_TOKEN = os.environ.get('SUPERVISOR_TOKEN', '')

Path(AUTOMATIONS_PATH).mkdir(parents=True, exist_ok=True)

print(f"Config path: {HA_CONFIG_PATH}")
print(f"Automations path: {AUTOMATIONS_PATH}")
print(f"Supervisor token available: {bool(SUPERVISOR_TOKEN)}")


def sanitize_filename(name):
    name = name.lower()
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[-\s]+', '_', name)
    return name[:50]


def parse_trigger_from_yaml(trigger_list):
    if not trigger_list or len(trigger_list) == 0:
        return {}
    
    trigger = trigger_list[0]
    platform = trigger.get('platform', '')
    
    result = {'type': platform}
    
    if platform == 'time':
        result['type'] = 'time'
        result['value'] = trigger.get('at', '')
    
    elif platform == 'template':
        template = trigger.get('value_template', '')
        if 'now().weekday()' in template:
            result['type'] = 'weekday'
            result['time'] = trigger.get('at', '12:00')
            result['weekdays'] = ['mon', 'tue', 'wed', 'thu', 'fri']
    
    elif platform == 'sun':
        result['type'] = 'sun'
        result['sunEvent'] = trigger.get('event', 'sunrise')
        offset = trigger.get('offset', '')
        if offset:
            result['sunOffset'] = 'before' if offset.startswith('-') else 'after'
            parts = offset.replace('-', '').replace('+', '').split(':')
            if len(parts) >= 2:
                result['sunMinutes'] = str(int(parts[1]))
            else:
                result['sunMinutes'] = '0'
        else:
            result['sunOffset'] = 'after'
            result['sunMinutes'] = '0'
    
    elif platform == 'state':
        entity = trigger.get('entity_id', '')
        to_state = trigger.get('to', '')
        result['value'] = entity
        
        if to_state == 'on':
            result['type'] = 'motion'
        else:
            result['type'] = 'state'
            result['to'] = to_state
    
    elif platform == 'numeric_state':
        result['type'] = 'numeric_state'
        result['value'] = trigger.get('entity_id', '')
        result['above'] = str(trigger.get('above', ''))
        result['below'] = str(trigger.get('below', ''))
    
    elif platform == 'zone':
        result['type'] = 'zone'
        result['value'] = trigger.get('entity_id', '')
        result['zone'] = trigger.get('zone', 'zone.home')
        result['event'] = trigger.get('event', 'enter')
    
    return result


def parse_condition_from_yaml(condition_list):
    if not condition_list or len(condition_list) == 0:
        return None
    
    condition = condition_list[0]
    condition_type = condition.get('condition', '')
    
    result = {}
    
    if condition_type == 'time':
        result['type'] = 'time'
        result['after'] = condition.get('after', '')
        result['before'] = condition.get('before', '')
    
    elif condition_type == 'state':
        result['type'] = 'state'
        result['entity'] = condition.get('entity_id', '')
        result['state'] = condition.get('state', '')
    
    elif condition_type == 'template':
        template = condition.get('value_template', '')
        if 'now().weekday()' in template:
            result['type'] = 'weekday'
            result['weekdays'] = ['mon', 'tue', 'wed', 'thu', 'fri']
    
    return result if result else None


def parse_action_from_yaml(action_list):
    if not action_list or len(action_list) == 0:
        return {}
    
    action = action_list[0]
    service = action.get('service', '')
    
    result = {}
    
    if 'turn_on' in service:
        result['type'] = 'turn_on'
        result['value'] = action.get('target', {}).get('entity_id', '')
    
    elif 'turn_off' in service:
        result['type'] = 'turn_off'
        result['value'] = action.get('target', {}).get('entity_id', '')
    
    elif 'notify' in service:
        result['type'] = 'notify'
        result['value'] = action.get('data', {}).get('message', '')
    
    elif 'scene.turn_on' in service:
        result['type'] = 'scene'
        entity = action.get('target', {}).get('entity_id', '')
        result['value'] = entity.replace('scene.', '')
    
    return result


def get_ha_entities():
    if not SUPERVISOR_TOKEN:
        print("ERROR: No supervisor token available!")
        return []
    
    try:
        headers = {
            'Authorization': f'Bearer {SUPERVISOR_TOKEN}',
            'Content-Type': 'application/json',
        }
        
        print("Fetching entities from Home Assistant...")
        
        response = requests.get(
            'http://supervisor/core/api/states',
            headers=headers,
            timeout=10
        )
        
        print(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            states = response.json()
            entities = []
            
            print(f"Found {len(states)} entities")
            
            for state in states:
                entity_id = state.get('entity_id', '')
                attributes = state.get('attributes', {})
                friendly_name = attributes.get('friendly_name', entity_id)
                domain = entity_id.split('.')[0] if '.' in entity_id else ''
                
                if domain not in ['group', 'zone', 'script', 'automation', 'updater', 'sun', 'weather']:
                    entities.append({
                        'entity_id': entity_id,
                        'name': friendly_name,
                        'domain': domain,
                        'state': state.get('state', 'unknown')
                    })
            
            entities_sorted = sorted(entities, key=lambda x: x['name'].lower())
            
            print(f"Returning {len(entities_sorted)} filtered entities")
            
            return entities_sorted
        else:
            print(f"Failed to fetch entities: {response.status_code}")
            print(f"Response: {response.text[:200]}")
        
    except Exception as e:
        print(f"ERROR: Exception fetching entities: {e}")
    
    return []


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'ok',
        'supervisor_token': bool(SUPERVISOR_TOKEN),
        'config_path': HA_CONFIG_PATH,
        'automations_path': AUTOMATIONS_PATH
    })


@app.route('/api/entities', methods=['GET'])
def get_entities():
    print("\n=== GET /api/entities ===")
    entities = get_ha_entities()
    print(f"Returning {len(entities)} entities to frontend")
    
    if len(entities) == 0:
        print("WARNING: No entities found! Check supervisor token")
    
    return jsonify(entities)


@app.route('/api/automations', methods=['GET'])
def list_automations():
    try:
        files = []
        
        if not os.path.exists(AUTOMATIONS_PATH):
            print(f"Automations path does not exist: {AUTOMATIONS_PATH}")
            return jsonify([])
        
        for file in os.listdir(AUTOMATIONS_PATH):
            if file.endswith('.yaml'):
                filepath = os.path.join(AUTOMATIONS_PATH, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = yaml.safe_load(f)
                        if content and isinstance(content, list) and len(content) > 0:
                            files.append({
                                'filename': file,
                                'name': content[0].get('alias', 'Onbekend'),
                                'path': filepath
                            })
                except Exception as e:
                    print(f"Error reading {file}: {e}")
        
        return jsonify(files)
    
    except Exception as e:
        print(f"Error listing automations: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/automation/<filename>', methods=['GET'])
def get_automation(filename):
    try:
        filepath = os.path.join(AUTOMATIONS_PATH, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Automation niet gevonden'}), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            yaml_data = yaml.safe_load(f)
        
        if not yaml_data or not isinstance(yaml_data, list) or len(yaml_data) == 0:
            return jsonify({'error': 'Ongeldige automation'}), 400
        
        auto_yaml = yaml_data[0]
        
        automation = {
            'name': auto_yaml.get('alias', 'Onbekend'),
            'trigger': parse_trigger_from_yaml(auto_yaml.get('trigger', [])),
            'condition': parse_condition_from_yaml(auto_yaml.get('condition', [])),
            'action': parse_action_from_yaml(auto_yaml.get('action', []))
        }
        
        return jsonify({'automation': automation})
    
    except Exception as e:
        print(f"Error getting automation: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/automation', methods=['POST'])
def save_automation():
    try:
        data = request.json
        print(f"\n=== POST /api/automation ===")
        print(f"Received data: {data}")
        
        if not data or 'automation' not in data:
            return jsonify({'error': 'Geen automation data ontvangen'}), 400
        
        automation = data['automation']
        name = automation.get('name', 'unnamed')
        
        filename = f"{sanitize_filename(name)}.yaml"
        filepath = os.path.join(AUTOMATIONS_PATH, filename)
        
        print(f"Saving to: {filepath}")
        
        if os.path.exists(filepath):
            return jsonify({'error': f'Automation "{name}" bestaat al!'}), 409
        
        yaml_content = generate_automation_yaml(automation)
        print(f"Generated YAML:\n{yaml_content}")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        
        print(f"File saved successfully: {filepath}")
        
        reload_result = reload_automations()
        print(f"Reload result: {reload_result}")
        
        return jsonify({
            'success': True,
            'message': f'Automation "{name}" opgeslagen!',
            'filename': filename,
            'path': filepath
        })
    
    except Exception as e:
        print(f"ERROR saving automation: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/automation/<filename>', methods=['PUT'])
def update_automation(filename):
    try:
        data = request.json
        print(f"\n=== PUT /api/automation/{filename} ===")
        print(f"Received data: {data}")
        
        if not data or 'automation' not in data:
            return jsonify({'error': 'Geen automation data ontvangen'}), 400
        
        automation = data['automation']
        filepath = os.path.join(AUTOMATIONS_PATH, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Automation niet gevonden'}), 404
        
        yaml_content = generate_automation_yaml(automation)
        print(f"Generated YAML:\n{yaml_content}")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        
        print(f"File updated successfully: {filepath}")
        
        reload_result = reload_automations()
        print(f"Reload result: {reload_result}")
        
        return jsonify({
            'success': True,
            'message': f'Automation "{automation.get("name")}" bijgewerkt!',
            'filename': filename
        })
    
    except Exception as e:
        print(f"ERROR updating automation: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/automation/<filename>', methods=['DELETE'])
def delete_automation(filename):
    try:
        filepath = os.path.join(AUTOMATIONS_PATH, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Automation niet gevonden'}), 404
        
        os.remove(filepath)
        reload_automations()
        
        return jsonify({
            'success': True,
            'message': 'Automation verwijderd'
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def generate_automation_yaml(automation):
    trigger = automation['trigger']
    action = automation['action']
    condition = automation.get('condition')
    
    yaml_data = [{
        'alias': automation['name'],
        'description': 'Aangemaakt met Automation Maker',
        'trigger': [],
        'mode': 'single'
    }]
    
    trigger_config = {}
    
    if trigger['type'] == 'time':
        trigger_config = {
            'platform': 'time',
            'at': trigger['value']
        }
    
    elif trigger['type'] == 'weekday':
        trigger_config = {
            'platform': 'time',
            'at': trigger.get('time', '12:00')
        }
        
        weekday_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
        weekday_numbers = [str(weekday_map[day]) for day in trigger.get('weekdays', [])]
        
        if 'condition' not in yaml_data[0]:
            yaml_data[0]['condition'] = []
        
        yaml_data[0]['condition'].append({
            'condition': 'template',
            'value_template': f"{{{{ now().weekday() in [{', '.join(weekday_numbers)}] }}}}"
        })
    
    elif trigger['type'] == 'state':
        trigger_config = {
            'platform': 'state',
            'entity_id': trigger['value']
        }
        if trigger.get('to'):
            trigger_config['to'] = trigger['to']
    
    elif trigger['type'] == 'sun':
        trigger_config = {
            'platform': 'sun',
            'event': trigger.get('sunEvent', 'sunrise')
        }
        if trigger.get('sunMinutes') and trigger['sunMinutes'] != '0':
            sign = '-' if trigger.get('sunOffset') == 'before' else ''
            minutes = str(trigger['sunMinutes']).zfill(2)
            trigger_config['offset'] = f"{sign}00:{minutes}:00"
    
    elif trigger['type'] == 'motion':
        trigger_config = {
            'platform': 'state',
            'entity_id': trigger['value'],
            'to': 'on'
        }
    
    elif trigger['type'] == 'numeric_state':
        trigger_config = {
            'platform': 'numeric_state',
            'entity_id': trigger['value']
        }
        if trigger.get('above'):
            trigger_config['above'] = float(trigger['above'])
        if trigger.get('below'):
            trigger_config['below'] = float(trigger['below'])
    
    elif trigger['type'] == 'zone':
        trigger_config = {
            'platform': 'zone',
            'entity_id': trigger['value'],
            'zone': trigger.get('zone', 'zone.home'),
            'event': trigger.get('event', 'enter')
        }
    
    yaml_data[0]['trigger'].append(trigger_config)
    
    if condition:
        if 'condition' not in yaml_data[0]:
            yaml_data[0]['condition'] = []
        
        if condition['type'] == 'weekday':
            weekday_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
            weekday_numbers = [str(weekday_map[day]) for day in condition.get('weekdays', [])]
            
            yaml_data[0]['condition'].append({
                'condition': 'template',
                'value_template': f"{{{{ now().weekday() in [{', '.join(weekday_numbers)}] }}}}"
            })
        
        elif condition['type'] == 'time':
            yaml_data[0]['condition'].append({
                'condition': 'time',
                'after': condition.get('after', '00:00'),
                'before': condition.get('before', '23:59')
            })
        
        elif condition['type'] == 'state':
            yaml_data[0]['condition'].append({
                'condition': 'state',
                'entity_id': condition.get('entity', ''),
                'state': condition.get('state', 'on')
            })
    
    action_config = {}
    
    if action['type'] in ['turn_on', 'turn_off']:
        action_config = {
            'service': f"homeassistant.{action['type']}",
            'target': {
                'entity_id': action['value']
            }
        }
    
    elif action['type'] == 'notify':
        action_config = {
            'service': 'notify.notify',
            'data': {
                'message': action['value']
            }
        }
    
    elif action['type'] == 'scene':
        scene_name = action['value'].replace('scene.', '')
        action_config = {
            'service': 'scene.turn_on',
            'target': {
                'entity_id': f"scene.{scene_name}"
            }
        }
    
    yaml_data[0]['action'] = [action_config]
    
    return yaml.dump(yaml_data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def reload_automations():
    if not SUPERVISOR_TOKEN:
        print("Warning: No supervisor token, skipping reload")
        return False
    
    try:
        headers = {
            'Authorization': f'Bearer {SUPERVISOR_TOKEN}',
            'Content-Type': 'application/json',
        }
        
        response = requests.post(
            'http://supervisor/core/api/services/automation/reload',
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            print("Automations reloaded successfully")
            return True
        else:
            print(f"Failed to reload automations: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    
    except Exception as e:
        print(f"Error reloading automations: {e}")
        return False


if __name__ == '__main__':
    print("\n" + "="*50)
    print("Automation Maker Starting...")
    print("="*50)
    print(f"Config path: {HA_CONFIG_PATH}")
    print(f"Automations path: {AUTOMATIONS_PATH}")
    print(f"Supervisor token: {'Available' if SUPERVISOR_TOKEN else 'Missing'}")
    print("="*50 + "\n")
    
    entities = get_ha_entities()
    print(f"\nFound {len(entities)} entities from Home Assistant\n")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
