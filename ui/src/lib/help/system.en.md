## System

- **Health** shows every service and [adapter](/help/adapters), with the reason next to anything that is not healthy. An adapter reports its own status, so a bad credential says so instead of looking merely quiet.
- **Alerts** are thresholds on the house itself: an adapter down, a flat battery, a sensor gone silent. A firing alert is visible in the app and can notify.
- **Network** is which interface the house's own devices are reached on, and **Wall panel** the tablet that shows the panel view. Which routes are published to the internet is set on the Cloudflare adapter, under [Adapters](/help/adapters).
- **Keys** holds every stored key — the assistant's LLM keys, the location-sharing key — each with a Test button.
- **Translations** edits the display names adapters supply, without touching the device.

## Data

- **Retention** decides how long each kind of history is kept. Raw readings are dense and short-lived; the daily rollups they feed are kept far longer.
- **Backup** makes a full database dump on a schedule and on demand, and restores one.
- **Commands** is the audit trail: who or what sent every command ([History](/help/history)).
- **Logs** shows what the services themselves wrote.
