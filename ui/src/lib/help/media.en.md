One page for everything that makes sound. A space picker on top (the rooms with a configured source), a source picker under it, and a body that changes to whatever is playing.

- Players are ordinary entities with the media capabilities (transport, volume, source), so an automation or the assistant can drive them like any other device.
- The music itself is not DIDA's. The records and the radio stations live in **OPUS · Player**, which this page asks on the spot. Picking a record or a station puts it on the space's player, and the device fetches the sound from OPUS directly.

## The special players

- `radio:tuner` — a synthetic entity that automations and remotes use to step through the stations (next, previous) or pin one by name on the configured player.
- `opus:tv` — the OPUS app on the living-room television, as a player: what it is playing (a film, an episode, a song or a station), its keys, and records or stations put on it. It takes orders only while OPUS is open on the television.
- `opus:dac` — the DAC on the OPUS server, whose sound goes to the amplifier's MUSIC input: the house's music and its radio (`radio:tuner` plays there). Its quality badge opens the signal path — the file as the library measured it against the format the DAC is fed.

## Speaking is not playing

Speaking to the house is separate from playing to it. An `announce:<speaker>` entity takes a `say` command with the text, and interrupts nothing permanently.
