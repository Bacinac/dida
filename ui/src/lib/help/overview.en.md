DIDA runs the house. Devices arrive through **adapters** — one per protocol, each in its own process — and the core only ever knows canonical **capabilities** (on_off, brightness, temperature…), never a concrete protocol. So a light behaves the same whether it speaks Zigbee, Wi-Fi or Matter, and a rule written for it keeps working when the device is replaced.

Its sibling BABA is the camera and vision system. The two share one message bus, so what BABA sees becomes ordinary readings here (see [Cameras](/help/cameras)).

## The pages

- **Floor plan** — the main surface. Devices sit on the plan of each floor, and this is where day-to-day control happens.
- **Entry** — a minimal page with the gate and the lock, for people who should not get the whole house ([Entry](/help/entry)).
- **Cameras** — live camera views ([Cameras](/help/cameras)).
- **Media** — spaces, sources, and the OPUS shelf and radio played into the house ([Media](/help/media)).
- **Heating** — room targets, schedules and the boiler ([Heating](/help/heating)).
- **Energy** — history and energy charts: the page where past readings live ([History](/help/history)).
- **Automations** — the rules, with schedules under them ([Automations](/help/automations)).
- **Assistant** — the chat that opens over any page ([Assistant](/help/assistant)).
- **Settings** — in groups: Devices (devices, adapters, helpers, scenes), Space (floor plans, rooms, zones), Users (users, address book), Data (retention, backup, commands, logs) and System (health, alerts, network, wall panel, keys, translations). Most of Settings is for admins only.

The **?** in the top bar opens the article about the page you are on.
