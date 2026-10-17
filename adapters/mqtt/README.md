# Zigbee2MQTT

DIDA reads Zigbee devices through [Zigbee2MQTT](https://www.zigbee2mqtt.io):
Zigbee2MQTT owns the coordinator and the Zigbee network, and DIDA's `mqtt`
adapter turns what it publishes into DIDA devices. Nothing is configured per
device.

## What the adapter does

- **Devices without configuration.** It reads the retained
  `<prefix>/bridge/devices` and maps each device's exposes to DIDA
  capabilities (on/off, brightness, colour temperature, temperature, contact
  and so on), with units normalised. A device paired later appears the moment
  Zigbee2MQTT announces it, and a device renamed in Zigbee2MQTT stays the same
  device in DIDA, recognised by its IEEE address.
- **Commands** go to `<prefix>/<friendly name>/set`.
- **Availability.** Zigbee2MQTT's own availability reports mark a device
  unavailable in DIDA. Whether that reporting is on is shown, and switched,
  in DIDA.
- **Refused commands.** Zigbee2MQTT reports a command the device refused only
  in its log. The adapter reads that log and records the refusal as an event
  on the device, so it does not pass silently.

## Connecting

Settings → Adapters → MQTT:

| Field | Value |
|---|---|
| MQTT URL | the broker Zigbee2MQTT publishes to, e.g. `mqtt://mosquitto:1883` |
| MQTT user / MQTT password | that broker's credentials |
| Topic prefix | Zigbee2MQTT's `base_topic`, `zigbee2mqtt` by default |

**An existing Zigbee2MQTT** keeps running where it is. Point the adapter at its
broker and the devices appear.

**Zigbee2MQTT run by DIDA.** DIDA ships it as the `zigbee` compose profile,
publishing to DIDA's own Mosquitto (`mqtt://mosquitto:1883`, the
`DIDA_MQTT_USERNAME` / `DIDA_MQTT_PASSWORD` account). Set the coordinator in
`.env` (`DIDA_ZIGBEE_DEVICE` for a USB stick; a network coordinator needs
nothing there) and in `state/zigbee2mqtt/configuration.yaml`, then start it
from the MQTT card. Start it only once no other owner (ZHA, another
Zigbee2MQTT) holds the coordinator. The MQTT card then also carries the
network's tools, so the Zigbee2MQTT frontend is needed only for its own
settings:

- open pairing (permit join) for 254 seconds;
- rename or remove a device;
- the network map, scanned on demand (a scan is slow and loads the mesh);
- a link to the Zigbee2MQTT frontend, and its auth token to copy.
