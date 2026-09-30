DIDA exposes entities over **Matter**, as a bridge. A voice assistant sees lights, plugs, covers and thermostats and can drive them, while DIDA stays the system of record.

What is exposed follows a rule: a controllable light, switch or cover is exposed on its own, so a new device shows up in Google Home without anyone curating it. [Settings → Devices](/help/devices) carries a per-entity override for the exceptions — a raw pulse relay is hidden, and the sequenced helper that drives it properly is exposed.

An AV activity (watch a film, play a console) is exposed as a plug rather than a light, deliberately: "turn off the lights" must not tear down what you are watching.

Voice commands arrive as ordinary commands and are recorded in the [audit trail](/help/history) like any other source.
