# Eufy C30 MQTT Home Assistant Add-on

Home Assistant add-on repository for publishing Eufy C30 doorbell events and snapshots to MQTT using an existing `eufy-security-ws` instance.

> This add-on does **not** implement the Eufy Android app protocol directly. It connects to `eufy-security-ws`, which acts as the unofficial Eufy app/client layer. This keeps the add-on focused on MQTT discovery, event mapping and snapshots.

## Features

- Connects to an existing `eufy-security-ws` host
- Publishes Home Assistant MQTT discovery entities
- Publishes ring, motion and person binary sensors
- Publishes battery/status sensors when present in events
- Saves latest snapshot to `/share/eufy_c30_mqtt/latest.jpg`
- Publishes snapshot bytes to an MQTT camera topic
- Publishes raw Eufy events to a debug topic
- Auto reconnects to MQTT and websocket

## Install as a Home Assistant add-on repository

1. Upload this repository to GitHub.
2. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
3. Open the three-dot menu and choose **Repositories**.
4. Add your GitHub repository URL.
5. Install **Eufy C30 MQTT Bridge**.

## Required

- A running MQTT broker, for example the Mosquitto broker add-on.
- A running `eufy-security-ws` instance on another Home Assistant/server.
- Network access from this add-on to `eufy-security-ws`.

## Configuration example

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_username: mqttuser
mqtt_password: password
mqtt_base_topic: eufy/c30
mqtt_discovery_prefix: homeassistant

# IP/hostname of the other Home Assistant where eufy-security-ws runs
eufy_ws_url: ws://192.168.1.50:3000

# Optional: filter to one device. Leave empty to accept first C30-looking doorbell events.
device_serial: ""
device_name: Eufy C30 Doorbell

snapshot_enabled: true
snapshot_http_url: ""
snapshot_interval_seconds: 0
raw_events: true
```

## Snapshot notes

Eufy doorbell snapshots depend on what `eufy-security-ws` exposes for your model and version. This add-on supports multiple strategies:

1. If the websocket event contains a snapshot URL, it downloads it.
2. If the event contains base64 JPEG data, it decodes it.
3. If `snapshot_http_url` is configured, it fetches that URL on ring/motion/person events.

Use `raw_events: true` and watch MQTT topic `eufy/c30/raw` to see which fields your `eufy-security-ws` sends.

## MQTT topics

Default base topic: `eufy/c30`

- `eufy/c30/ring/state`
- `eufy/c30/motion/state`
- `eufy/c30/person/state`
- `eufy/c30/battery/state`
- `eufy/c30/snapshot/image`
- `eufy/c30/raw`
- `eufy/c30/status`

## Development

The add-on is intentionally small and readable. Main code lives in:

```text
eufy_c30_mqtt/rootfs/app/main.py
```

