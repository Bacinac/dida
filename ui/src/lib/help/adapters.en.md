One adapter per protocol, each in its own process. If an adapter dies, its entities go unavailable and everything else keeps running; the runtime restarts it.

DIDA does not reimplement protocols — it consumes the bridges that already do them well (zigbee2mqtt, ESPHome, Matter and so on).

Adapters are configured in Settings → Adapters, where each one reports its status. An adapter that is not healthy says why — a bad credential says so instead of looking merely quiet (see [System](/help/system)). Secrets entered there are stored encrypted and never shown back.
