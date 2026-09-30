An [adapter](/help/adapters) publishes **entities**, and each entity exposes a set of **capabilities**. The core knows only those capabilities, never the device behind them.

The **device type** — light, switch, cover, lock, media, remote, presence, sensor, button — is DIDA's own classification. The adapter only seeds it once; after that, the value set in Settings wins and is never overwritten.

For each entity you can:

- choose which of its fields are shown,
- hide it,
- mark it for [voice assistants](/help/voice).

A hidden entity is hidden everywhere for the users it is hidden from — including the assistant, which will neither name it nor act on it.
