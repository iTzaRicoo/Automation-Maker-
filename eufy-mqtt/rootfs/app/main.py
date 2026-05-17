import json
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
import websocket


CONFIG_PATH = "/data/options.json"

with open(CONFIG_PATH, "r") as f:
    cfg = json.load(f)

MQTT_HOST = cfg.get("mqtt_host", "homeassistant")
MQTT_PORT = int(cfg.get("mqtt_port", 1883))
MQTT_USER = cfg.get("mqtt_user", "")
MQTT_PASSWORD = cfg.get("mqtt_password", "")
TOPIC_PREFIX = cfg.get("mqtt_topic_prefix", "eufy/c30").rstrip("/")

EUFY_HOST = cfg.get("eufy_host", "")
EUFY_PORT = int(cfg.get("eufy_port", 3000))

DISCOVERY_PREFIX = "homeassistant"

SNAPSHOT_DIR = Path("/share/eufy_c30_mqtt")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_FILE = SNAPSHOT_DIR / "latest.jpg"


def log(*args):
    print(*args, flush=True)


mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASSWORD)

log(f"Connecting to MQTT {MQTT_HOST}:{MQTT_PORT}")

mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
mqttc.loop_start()


def publish_discovery():
    log("Publishing MQTT discovery")

    device = {
        "identifiers": ["eufy_c30"],
        "name": "Eufy C30 Doorbell",
        "manufacturer": "Eufy",
        "model": "C30",
    }

    camera_payload = {
        "name": "Eufy C30 Snapshot",
        "unique_id": "eufy_c30_snapshot",
        "topic": f"{TOPIC_PREFIX}/snapshot",
        "image_encoding": "b64",
        "device": device,
    }

    mqttc.publish(
        f"{DISCOVERY_PREFIX}/camera/eufy_c30_snapshot/config",
        json.dumps(camera_payload),
        retain=True,
    )

    sensors = [
        ("motion", "Motion", "motion"),
        ("ring", "Ring", None),
        ("person", "Person Detection", "motion"),
    ]

    for key, label, device_class in sensors:
        payload = {
            "name": f"Eufy C30 {label}",
            "state_topic": f"{TOPIC_PREFIX}/{key}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "unique_id": f"eufy_c30_{key}",
            "device": device,
        }

        if device_class:
            payload["device_class"] = device_class

        mqttc.publish(
            f"{DISCOVERY_PREFIX}/binary_sensor/eufy_c30_{key}/config",
            json.dumps(payload),
            retain=True,
        )


def pulse(topic):
    mqttc.publish(topic, "ON")
    time.sleep(2)
    mqttc.publish(topic, "OFF")


def publish_snapshot_from_url(image_url):
    try:
        log("Fetching snapshot:", image_url)

        r = requests.get(image_url, timeout=20)

        if r.status_code != 200:
            log("Snapshot HTTP error:", r.status_code)
            return

        SNAPSHOT_FILE.write_bytes(r.content)

        mqttc.publish(
            f"{TOPIC_PREFIX}/snapshot",
            r.content
        )

        log("Snapshot published:", SNAPSHOT_FILE)

    except Exception as e:
        log("Snapshot error:", e)


def find_snapshot_url(data):
    candidates = [
        "snapshot",
        "snapshotUrl",
        "image",
        "imageUrl",
        "picture",
        "pictureUrl",
        "thumbnail",
        "thumbnailUrl",
        "eventImage",
        "eventImageUrl",
    ]

    for key in candidates:
        value = data.get(key)

        if isinstance(value, str) and value.startswith("http"):
            return value

    payload = data.get("payload")

    if isinstance(payload, dict):
        for key in candidates:
            value = payload.get(key)

            if isinstance(value, str) and value.startswith("http"):
                return value

    return None


def detect_event(data):
    text = json.dumps(data).lower()

    is_motion = any(x in text for x in [
        "motion",
        "pir",
        "detected"
    ])

    is_ring = any(x in text for x in [
        "ring",
        "doorbell",
        "ding",
        "press"
    ])

    is_person = any(x in text for x in [
        "person",
        "human",
        "ai_person"
    ])

    return is_motion, is_ring, is_person


def on_message(ws, message):
    log("RAW MESSAGE:", message)

    try:
        data = json.loads(message)

    except Exception as e:
        log("JSON parse error:", e)

        mqttc.publish(
            f"{TOPIC_PREFIX}/raw",
            str(message)
        )

        return

    mqttc.publish(
        f"{TOPIC_PREFIX}/raw",
        json.dumps(data)
    )

    is_motion, is_ring, is_person = detect_event(data)

    if is_motion:
        log("Detected motion event")

        pulse(f"{TOPIC_PREFIX}/motion")

    if is_ring:
        log("Detected ring event")

        pulse(f"{TOPIC_PREFIX}/ring")

    if is_person:
        log("Detected person event")

        pulse(f"{TOPIC_PREFIX}/person")

    image_url = find_snapshot_url(data)

    if image_url:
        publish_snapshot_from_url(image_url)


def on_open(ws):
    log("Connected to eufy-security-ws")

    subscribe_messages = [
        {
            "command": "subscribe",
            "type": "event"
        },
        {
            "command": "subscribe",
            "type": "device"
        },
        {
            "command": "subscribe",
            "type": "station"
        }
    ]

    for msg in subscribe_messages:
        try:
            ws.send(json.dumps(msg))

            log("Sent subscribe:", msg)

        except Exception as e:
            log("Subscribe error:", e)


def on_error(ws, error):
    log("WebSocket error:", error)


def on_close(ws, close_status_code, close_msg):
    log("WebSocket closed:", close_status_code, close_msg)


def connect():
    publish_discovery()

    ws_url = f"ws://{EUFY_HOST}:{EUFY_PORT}"

    while True:
        try:
            log(f"Connecting to {ws_url}")

            ws = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            ws.run_forever()

        except Exception as e:
            log("WebSocket loop error:", e)

        log("Reconnect in 10 seconds")

        time.sleep(10)


if __name__ == "__main__":
    connect()
