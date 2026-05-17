import json
import os
import ssl
import time
import requests
import websocket
import paho.mqtt.client as mqtt

from pathlib import Path

CONFIG_PATH = "/data/options.json"

with open(CONFIG_PATH, "r") as f:
    cfg = json.load(f)

MQTT_HOST = cfg["mqtt_host"]
MQTT_PORT = cfg["mqtt_port"]
MQTT_USER = cfg["mqtt_user"]
MQTT_PASSWORD = cfg["mqtt_password"]
TOPIC_PREFIX = cfg["mqtt_topic_prefix"]

EUFY_HOST = cfg["eufy_host"]
EUFY_PORT = cfg["eufy_port"]

SNAPSHOT_DIR = Path("/share/eufy_c30_mqtt")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOT_FILE = SNAPSHOT_DIR / "latest.jpg"

mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASSWORD)

mqttc.connect(MQTT_HOST, MQTT_PORT, 60)

DISCOVERY_PREFIX = "homeassistant"

def publish_discovery():
    discovery = {
        "name": "Eufy C30 Snapshot",
        "unique_id": "eufy_c30_snapshot",
        "topic": f"{TOPIC_PREFIX}/snapshot",
        "image_encoding": "b64",
        "device": {
            "identifiers": ["eufy_c30"],
            "name": "Eufy C30 Doorbell",
            "manufacturer": "Eufy",
            "model": "C30"
        }
    }

    mqttc.publish(
        f"{DISCOVERY_PREFIX}/camera/eufy_c30/config",
        json.dumps(discovery),
        retain=True
    )

    sensors = [
        ("motion", "Motion"),
        ("ring", "Doorbell Ring"),
        ("person", "Person Detection"),
    ]

    for key, label in sensors:
        payload = {
            "name": f"Eufy C30 {label}",
            "state_topic": f"{TOPIC_PREFIX}/{key}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "unique_id": f"eufy_c30_{key}",
            "device_class": "motion",
            "device": {
                "identifiers": ["eufy_c30"]
            }
        }

        mqttc.publish(
            f"{DISCOVERY_PREFIX}/binary_sensor/eufy_c30_{key}/config",
            json.dumps(payload),
            retain=True
        )

def publish_snapshot(image_url):
    try:
        r = requests.get(image_url, timeout=20)

        if r.status_code == 200:
            SNAPSHOT_FILE.write_bytes(r.content)

            mqttc.publish(
                f"{TOPIC_PREFIX}/snapshot",
                r.content
            )

            print("Snapshot published")

    except Exception as e:
        print("Snapshot error:", e)

def on_message(ws, message):
    try:
        data = json.loads(message)

        mqttc.publish(
            f"{TOPIC_PREFIX}/raw",
            json.dumps(data)
        )

        event_type = data.get("type", "")

        if "motion" in event_type.lower():
            mqttc.publish(f"{TOPIC_PREFIX}/motion", "ON")
            time.sleep(2)
            mqttc.publish(f"{TOPIC_PREFIX}/motion", "OFF")

        if "ring" in event_type.lower():
            mqttc.publish(f"{TOPIC_PREFIX}/ring", "ON")
            time.sleep(2)
            mqttc.publish(f"{TOPIC_PREFIX}/ring", "OFF")

        if "person" in event_type.lower():
            mqttc.publish(f"{TOPIC_PREFIX}/person", "ON")
            time.sleep(2)
            mqttc.publish(f"{TOPIC_PREFIX}/person", "OFF")

        image_url = data.get("snapshot")

        if image_url:
            publish_snapshot(image_url)

    except Exception as e:
        print("Event parse error:", e)

def connect():
    publish_discovery()

    ws_url = f"ws://{EUFY_HOST}:{EUFY_PORT}"

    while True:
        try:
            print(f"Connecting to {ws_url}")

            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message
            )

            ws.run_forever()

        except Exception as e:
            print("Websocket error:", e)

        time.sleep(10)

if __name__ == "__main__":
    connect()
