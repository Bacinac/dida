"""DIDA MQTT adapter.

Bridges an MQTT broker (zigbee2mqtt / zwave-js publish here) onto the DIDA bus.
Native device JSON is mapped to canonical capability state updates; capability
commands are mapped back to the broker's `.../set` convention.

This adapter knows MQTT; the engine never does. It runs as its own container,
so a broker outage or a malformed payload storm stays contained here.
"""

from __future__ import annotations

from dida_adapter_mqtt.adapter import MqttAdapter

__all__ = ["MqttAdapter"]
