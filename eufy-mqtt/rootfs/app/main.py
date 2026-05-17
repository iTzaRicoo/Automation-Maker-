import base64
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

DEVICE_SERIAL = cfg.get("device_serial", "").strip()
STATION_SERIAL = cfg.get("station_serial", "").strip()

DISCOVERY_PREFIX = "homeassistant"

SNAPSHOT_DIR = Path("/share/eufy_c30_mqtt")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_FILE = SNAPSHOT_DIR / "latest.jpg"

ws_app = None


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

    mqttc.publish(
        f"{DISCOVERY_PREFIX}/camera/eufy_c30_snapshot/config",
        json.dumps({
            "name": "Eufy C30 Snapshot",
            "unique_id": "eufy_c30_snapshot",
            "topic": f"{TOPIC_PREFIX}/snapshot",
            "image_encoding": "b64",
            "device": device,
        }),
        retain=True,
    )

    sensors = [
        ("motion", "Motion", "motion"),
        ("person", "Person Detection", "motion"),
        ("ring", "Ring", None),
        ("connection_error", "Connection Error", "problem"),
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


def mqtt_state(key, value):
    mqttc.publish(f"{TOPIC_PREFIX}/{key}", value, retain=False)


def pulse(key, seconds=2):
    mqtt_state(key, "ON")
    time.sleep(seconds)
    mqtt_state(key, "OFF")


def publish_snapshot_bytes(image_bytes):
    if not image_bytes or len(image_bytes) < 10:
        log("Snapshot invalid or empty")
        return

    SNAPSHOT_FILE.write_bytes(image_bytes)

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    mqttc.publish(f"{TOPIC_PREFIX}/snapshot", encoded)

    log("Snapshot published:", SNAPSHOT_FILE, "bytes:", len(image_bytes))


def publish_snapshot_from_url(image_url):
    try:
        log("Fetching snapshot URL:", image_url)
        r = requests.get(image_url, timeout=20)

        if r.status_code != 200:
            log("Snapshot HTTP error:", r.status_code)
            return

        publish_snapshot_bytes(r.content)

    except Exception as e:
        log("Snapshot URL error:", e)


def extract_buffer_bytes(value):
    if isinstance(value, dict):
        if value.get("type") == "Buffer" and isinstance(value.get("data"), list):
            return bytes(value["data"])

        if isinstance(value.get("data"), list):
            return bytes(value["data"])

        if isinstance(value.get("data"), dict):
            return extract_buffer_bytes(value["data"])

    if isinstance(value, list):
        return bytes(value)

    return None


def find_snapshot_in_object(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).lower()

            if key_lower in [
                "picture",
                "snapshot",
                "thumbnail",
                "image",
                "eventimage",
                "event_image",
            ]:
                img = extract_buffer_bytes(value)
                if img:
                    return img

                if isinstance(value, str) and value.startswith("http"):
                    publish_snapshot_from_url(value)

            result = find_snapshot_in_object(value)
            if result:
                return result

    if isinstance(obj, list):
        for item in obj:
            result = find_snapshot_in_object(item)
            if result:
                return result

    return None


def send_ws(command):
    global ws_app

    if ws_app is None:
        log("WS not ready, cannot send:", command)
        return

    try:
        ws_app.send(json.dumps(command))
        log("Sent command:", command)

    except Exception as e:
        log("WS send error:", e)


def request_device_properties(serial):
    if not serial:
        return

    send_ws({
        "messageId": f"device_properties_{int(time.time() * 1000)}",
        "command": "device.get_properties",
        "serialNumber": serial,
    })


def request_device_properties_metadata(serial):
    if not serial:
        return

    send_ws({
        "messageId": f"device_properties_metadata_{int(time.time() * 1000)}",
        "command": "device.get_properties_metadata",
        "serialNumber": serial,
    })


def update_serials_from_state(result):
    global DEVICE_SERIAL, STATION_SERIAL

    state = result.get("state", {})
    devices = state.get("devices", [])
    stations = state.get("stations", [])

    if not DEVICE_SERIAL and devices:
        DEVICE_SERIAL = devices[0]
        log("Auto device serial:", DEVICE_SERIAL)

    if not STATION_SERIAL and stations:
        STATION_SERIAL = stations[0]
        log("Auto station serial:", STATION_SERIAL)


def detect_event(data):
    event = data.get("event", {})

    if not isinstance(event, dict):
        return False, False, False, False

    event_name = str(event.get("event", "")).lower()
    prop_name = str(event.get("name", "")).lower()
    value = event.get("value")
    state = event.get("state")

    is_motion = False
    is_person = False
    is_ring = False
    is_connection_error = False

    if event_name == "connection error":
        is_connection_error = True

    if event_name == "property changed":
        if prop_name == "ringing" and value is True:
            is_ring = True

        if prop_name == "motiondetected" and value is True:
            is_motion = True

        if prop_name == "persondetected" and value is True:
            is_person = True

        if prop_name == "picture" and value:
            img = extract_buffer_bytes(value)
            if img:
                publish_snapshot_bytes(img)
            elif isinstance(value, str) and value.startswith("http"):
                publish_snapshot_from_url(value)

    if event_name == "rings" and state is True:
        is_ring = True

    return is_motion, is_person, is_ring, is_connection_error


def handle_result(data):
    message_id = str(data.get("messageId", ""))
    result = data.get("result", {})

    if message_id == "start_listening":
        update_serials_from_state(result)

        if DEVICE_SERIAL:
            request_device_properties_metadata(DEVICE_SERIAL)
            request_device_properties(DEVICE_SERIAL)

    img = find_snapshot_in_object(result)
    if img:
        publish_snapshot_bytes(img)

    if isinstance(result, dict) and "properties" in result:
        props = result["properties"]

        motion = props.get("motionDetected")
        person = props.get("personDetected")
        ringing = props.get("ringing")
        picture = props.get("picture")

        if motion is True:
            log("PROPERTY motionDetected TRUE")
            pulse("motion", 2)

        if person is True:
            log("PROPERTY personDetected TRUE")
            pulse("person", 2)

        if ringing is True:
            log("PROPERTY ringing TRUE")
            pulse("ring", 2)

        if picture:
            log("PROPERTY picture found")

            img = extract_buffer_bytes(picture)

            if img:
                publish_snapshot_bytes(img)

            elif isinstance(picture, str) and picture.startswith("http"):
                publish_snapshot_from_url(picture)


def poll_after_event(reason):
    if not DEVICE_SERIAL:
        return

    log(f"Polling after {reason}")

    for i in range(10):
        time.sleep(2)
        log(f"Polling properties attempt {i + 1}/10")
        request_device_properties(DEVICE_SERIAL)


def on_message(ws, message):
    log("RAW MESSAGE:", message)

    try:
        data = json.loads(message)
    except Exception as e:
        log("JSON parse error:", e)
        mqttc.publish(f"{TOPIC_PREFIX}/raw", str(message))
        return

    mqttc.publish(f"{TOPIC_PREFIX}/raw", json.dumps(data))

    if data.get("type") == "result":
        handle_result(data)

    img = find_snapshot_in_object(data)
    if img:
        publish_snapshot_bytes(img)

    is_motion, is_person, is_ring, is_connection_error = detect_event(data)

    if is_connection_error:
        log("Detected station connection error")
        pulse("connection_error", 2)

    if is_ring:
        log("Detected ring event")
        pulse("ring", 2)
        poll_after_event("ring")

    if is_motion:
        log("Detected motion event")
        pulse("motion", 2)
        poll_after_event("motion")

    if is_person:
        log("Detected person event")
        pulse("person", 2)
        poll_after_event("person")


def on_open(ws):
    log("Connected to eufy-security-ws")

    startup_messages = [
        {
            "messageId": "set_api_schema",
            "command": "set_api_schema",
            "schemaVersion": 21,
        },
        {
            "messageId": "start_listening",
            "command": "start_listening",
        },
    ]

    for msg in startup_messages:
        send_ws(msg)


def on_error(ws, error):
    log("WebSocket error:", error)


def on_close(ws, close_status_code, close_msg):
    log("WebSocket closed:", close_status_code, close_msg)


def connect():
    global ws_app

    publish_discovery()

    ws_url = f"ws://{EUFY_HOST}:{EUFY_PORT}"

    while True:
        try:
            log(f"Connecting to {ws_url}")

            ws_app = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            ws_app.run_forever()

        except Exception as e:
            log("WebSocket loop error:", e)

        log("Reconnect in 10 seconds")
        time.sleep(10)


if __name__ == "__main__":
    connect()
