import base64
import json
import time
from datetime import datetime, timedelta, timezone
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

DEVICE_SERIAL = cfg.get("device_serial", "").strip()
STATION_SERIAL = cfg.get("station_serial", "").strip()

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
    if not image_bytes:
        log("Snapshot empty")
        return

    if len(image_bytes) < 10:
        log("Snapshot too small:", len(image_bytes))
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

        if isinstance(value.get("data"), dict):
            return extract_buffer_bytes(value["data"])

        if isinstance(value.get("data"), list):
            return bytes(value["data"])

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
                    return None

            result = find_snapshot_in_object(value)
            if result:
                return result

    elif isinstance(obj, list):
        for item in obj:
            result = find_snapshot_in_object(item)
            if result:
                return result

    return None


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
        "messageId": f"device_properties_{int(time.time())}",
        "command": "device.get_properties",
        "serialNumber": serial,
    })


def request_device_properties_metadata(serial):
    if not serial:
        return

    send_ws({
        "messageId": f"device_properties_metadata_{int(time.time())}",
        "command": "device.get_properties_metadata",
        "serialNumber": serial,
    })


def request_recent_events(station_serial, device_serial=None):
    if not station_serial:
        return

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=10)

    cmd = {
        "messageId": f"database_query_{int(time.time())}",
        "command": "station.database_query_by_date",
        "serialNumber": station_serial,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
    }

    if device_serial:
        cmd["serialNumbers"] = [device_serial]

    send_ws(cmd)


def download_station_image(station_serial, file_name):
    if not station_serial or not file_name:
        return

    send_ws({
        "messageId": f"download_image_{int(time.time())}",
        "command": "station.download_image",
        "serialNumber": station_serial,
        "file": file_name,
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
    is_ring = False
    is_person = False
    is_connection_error = False

    if event_name == "connection error":
        is_connection_error = True

    if event_name == "property changed":
        if prop_name == "ringing" and value is True:
            is_ring = True

        if prop_name in [
            "motiondetected",
            "motion_detected",
            "motion",
            "motionsensorpir",
        ] and value is True:
            is_motion = True

        if prop_name in [
            "persondetected",
            "person_detected",
            "person",
            "human",
            "human_detected",
            "humandetected",
        ] and value is True:
            is_person = True

        if prop_name in [
            "picture",
            "snapshot",
            "thumbnail",
            "image",
        ]:
            img = extract_buffer_bytes(value)
            if img:
                publish_snapshot_bytes(img)

    if event_name == "rings" and state is True:
        is_ring = True

    if event_name in ["motion detected", "motion", "motion detected event"] and state is True:
        is_motion = True

    if event_name in ["person detected", "person", "human detected"] and state is True:
        is_person = True

    return is_motion, is_ring, is_person, is_connection_error


def handle_result(data):
    message_id = str(data.get("messageId", ""))
    result = data.get("result", {})

    if message_id == "start_listening":
        update_serials_from_state(result)

        if DEVICE_SERIAL:
            request_device_properties_metadata(DEVICE_SERIAL)
            request_device_properties(DEVICE_SERIAL)

        if STATION_SERIAL:
            request_recent_events(STATION_SERIAL, DEVICE_SERIAL)

    img = find_snapshot_in_object(result)
    if img:
        publish_snapshot_bytes(img)

    if isinstance(result, dict):
        for key in ["file", "filename", "path", "name"]:
            value = result.get(key)
            if isinstance(value, str) and STATION_SERIAL:
                download_station_image(STATION_SERIAL, value)


def on_message(ws, message):
    log("RAW MESSAGE:", message)

    try:
        data = json.loads(message)
    except Exception as e:
        log("JSON parse error:", e)
        mqttc.publish(f"{TOPIC_PREFIX}/raw", str(message))
        return

    mqttc.publish(f"{TOPIC_PREFIX}/raw", json.dumps(data))

    msg_type = data.get("type")

    if msg_type == "result":
        handle_result(data)

    image_url = find_snapshot_url(data)
    if image_url:
        publish_snapshot_from_url(image_url)

    img = find_snapshot_in_object(data)
    if img:
        publish_snapshot_bytes(img)

    is_motion, is_ring, is_person, is_connection_error = detect_event(data)

    if is_connection_error:
        log("Detected station connection error")
        pulse("connection_error", 2)

    if is_ring:
        log("Detected ring event")
        pulse("ring", 2)

        if DEVICE_SERIAL:
            time.sleep(1)
            request_device_properties(DEVICE_SERIAL)

        if STATION_SERIAL:
            request_recent_events(STATION_SERIAL, DEVICE_SERIAL)

    if is_motion:
        log("Detected motion event")
        pulse("motion", 2)

        if DEVICE_SERIAL:
            request_device_properties(DEVICE_SERIAL)

    if is_person:
        log("Detected person event")
        pulse("person", 2)

        if DEVICE_SERIAL:
            request_device_properties(DEVICE_SERIAL)


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
