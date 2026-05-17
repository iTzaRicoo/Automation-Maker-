import asyncio
import base64
import json
import os
import signal
import time
from typing import Any, Dict, Optional

import aiohttp
import paho.mqtt.client as mqtt
import websockets

CONFIG_PATH = "/data/options.json"
SNAPSHOT_DIR = "/share/eufy_c30_mqtt"
SNAPSHOT_PATH = f"{SNAPSHOT_DIR}/latest.jpg"


def log(message: str) -> None:
    print(f"[eufy-c30-mqtt] {message}", flush=True)


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class Bridge:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.base = cfg.get("mqtt_base_topic", "eufy/c30").strip("/")
        self.discovery_prefix = cfg.get("mqtt_discovery_prefix", "homeassistant").strip("/")
        self.device_name = cfg.get("device_name", "Eufy C30 Doorbell")
        self.device_serial = cfg.get("device_serial", "")
        self.raw_events = bool(cfg.get("raw_events", True))
        self.snapshot_enabled = bool(cfg.get("snapshot_enabled", True))
        self.last_event_time: Dict[str, float] = {}
        self.stop_event = asyncio.Event()

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="eufy-c30-mqtt")
        username = cfg.get("mqtt_username") or None
        password = cfg.get("mqtt_password") or None
        if username:
            self.mqtt.username_pw_set(username, password)
        self.mqtt.will_set(f"{self.base}/status", "offline", retain=True)

    def connect_mqtt(self) -> None:
        host = self.cfg["mqtt_host"]
        port = int(self.cfg.get("mqtt_port", 1883))
        log(f"Connecting MQTT to {host}:{port}")
        self.mqtt.connect(host, port, keepalive=60)
        self.mqtt.loop_start()
        self.publish_status("online")
        self.publish_discovery()

    def publish_status(self, value: str) -> None:
        self.mqtt.publish(f"{self.base}/status", value, retain=True)

    def device_payload(self) -> Dict[str, Any]:
        return {
            "identifiers": [self.device_serial or "eufy_c30_mqtt"],
            "name": self.device_name,
            "manufacturer": "Eufy",
            "model": "C30 Doorbell via eufy-security-ws",
        }

    def publish_discovery(self) -> None:
        dev = self.device_payload()
        binary_sensors = {
            "ring": ("Doorbell Ring", "None"),
            "motion": ("Motion", "motion"),
            "person": ("Person", "occupancy"),
        }
        for key, (name, device_class) in binary_sensors.items():
            topic = f"{self.discovery_prefix}/binary_sensor/eufy_c30_{key}/config"
            payload = {
                "name": f"{self.device_name} {name}",
                "unique_id": f"eufy_c30_{key}",
                "state_topic": f"{self.base}/{key}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": f"{self.base}/status",
                "device": dev,
            }
            if device_class != "None":
                payload["device_class"] = device_class
            self.mqtt.publish(topic, json.dumps(payload), retain=True)

        self.mqtt.publish(
            f"{self.discovery_prefix}/sensor/eufy_c30_battery/config",
            json.dumps({
                "name": f"{self.device_name} Battery",
                "unique_id": "eufy_c30_battery",
                "state_topic": f"{self.base}/battery/state",
                "unit_of_measurement": "%",
                "device_class": "battery",
                "availability_topic": f"{self.base}/status",
                "device": dev,
            }),
            retain=True,
        )

        self.mqtt.publish(
            f"{self.discovery_prefix}/camera/eufy_c30_snapshot/config",
            json.dumps({
                "name": f"{self.device_name} Snapshot",
                "unique_id": "eufy_c30_snapshot",
                "topic": f"{self.base}/snapshot/image",
                "availability_topic": f"{self.base}/status",
                "device": dev,
            }),
            retain=True,
        )
        log("Published MQTT discovery")

    def device_matches(self, event: Dict[str, Any]) -> bool:
        if not self.device_serial:
            return True
        text = json.dumps(event, default=str)
        return self.device_serial in text

    async def run(self) -> None:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        self.connect_mqtt()
        tasks = [asyncio.create_task(self.ws_loop())]
        if self.snapshot_enabled and int(self.cfg.get("snapshot_interval_seconds", 0)) > 0:
            tasks.append(asyncio.create_task(self.periodic_snapshot_loop()))
        await self.stop_event.wait()
        for t in tasks:
            t.cancel()
        self.publish_status("offline")
        self.mqtt.loop_stop()
        self.mqtt.disconnect()

    async def ws_loop(self) -> None:
        url = self.cfg["eufy_ws_url"]
        while not self.stop_event.is_set():
            try:
                log(f"Connecting websocket to {url}")
                async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                    log("Connected to eufy-security-ws")
                    async for message in ws:
                        await self.handle_ws_message(message)
            except Exception as e:
                log(f"Websocket error: {e}. Reconnecting soon.")
                await asyncio.sleep(10)

    async def handle_ws_message(self, message: Any) -> None:
        try:
            event = json.loads(message if isinstance(message, str) else message.decode())
        except Exception:
            log("Ignoring non-JSON websocket message")
            return

        if not self.device_matches(event):
            return

        if self.raw_events:
            self.mqtt.publish(f"{self.base}/raw", json.dumps(event), retain=False)

        await self.map_event(event)

    async def map_event(self, event: Dict[str, Any]) -> None:
        text = json.dumps(event, default=str).lower()

        if any(k in text for k in ["ring", "doorbell", "pressed", "ding"]):
            self.pulse_binary("ring")
            await self.try_snapshot(event)

        if "motion" in text:
            self.pulse_binary("motion")
            await self.try_snapshot(event)

        if any(k in text for k in ["person", "human"]):
            self.pulse_binary("person")
            await self.try_snapshot(event)

        battery = self.find_number(event, ["battery", "battery_level", "batteryLevel", "battery_percent"])
        if battery is not None:
            self.mqtt.publish(f"{self.base}/battery/state", str(int(battery)), retain=True)

    def pulse_binary(self, name: str, seconds: int = 30) -> None:
        now = time.time()
        self.last_event_time[name] = now
        self.mqtt.publish(f"{self.base}/{name}/state", "ON", retain=True)
        asyncio.create_task(self.turn_off_later(name, now, seconds))

    async def turn_off_later(self, name: str, event_time: float, seconds: int) -> None:
        await asyncio.sleep(seconds)
        if self.last_event_time.get(name) == event_time:
            self.mqtt.publish(f"{self.base}/{name}/state", "OFF", retain=True)

    def find_number(self, obj: Any, keys: list[str]) -> Optional[float]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and isinstance(v, (int, float)):
                    return float(v)
                found = self.find_number(v, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self.find_number(item, keys)
                if found is not None:
                    return found
        return None

    async def periodic_snapshot_loop(self) -> None:
        interval = int(self.cfg.get("snapshot_interval_seconds", 0))
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            await self.try_snapshot({})

    async def try_snapshot(self, event: Dict[str, Any]) -> None:
        if not self.snapshot_enabled:
            return
        image = self.extract_snapshot_bytes(event)
        if image is None:
            url = self.find_snapshot_url(event) or self.cfg.get("snapshot_http_url")
            if url:
                image = await self.download_snapshot(url)
        if image:
            with open(SNAPSHOT_PATH, "wb") as f:
                f.write(image)
            self.mqtt.publish(f"{self.base}/snapshot/image", image, retain=True)
            self.mqtt.publish(f"{self.base}/snapshot/path", SNAPSHOT_PATH, retain=True)
            log(f"Published snapshot ({len(image)} bytes)")

    def extract_snapshot_bytes(self, obj: Any) -> Optional[bytes]:
        candidates = ["snapshot", "snapshotBase64", "image", "imageBase64", "picture", "thumbnail"]
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in candidates and isinstance(v, str):
                    data = v.split(",", 1)[-1]
                    try:
                        decoded = base64.b64decode(data, validate=False)
                        if decoded.startswith(b"\xff\xd8"):
                            return decoded
                    except Exception:
                        pass
                found = self.extract_snapshot_bytes(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self.extract_snapshot_bytes(item)
                if found:
                    return found
        return None

    def find_snapshot_url(self, obj: Any) -> Optional[str]:
        keys = ["snapshotUrl", "snapshot_url", "imageUrl", "image_url", "thumbnailUrl", "thumbnail_url", "pictureUrl"]
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and isinstance(v, str) and v.startswith(("http://", "https://")):
                    return v
                found = self.find_snapshot_url(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self.find_snapshot_url(item)
                if found:
                    return found
        return None

    async def download_snapshot(self, url: str) -> Optional[bytes]:
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        log(f"Snapshot HTTP status {resp.status}")
                        return None
                    data = await resp.read()
                    if data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG"):
                        return data
                    log("Snapshot download did not look like an image")
        except Exception as e:
            log(f"Snapshot download error: {e}")
        return None


def main() -> None:
    cfg = load_config()
    bridge = Bridge(cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge.stop_event.set)

    try:
        loop.run_until_complete(bridge.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
