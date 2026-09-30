"""The assistant's knowledge of DIDA itself.

The assistant could drive devices but knew nothing ABOUT the app: asked "how do I
make a rule" or "where do I see who turned the light on", it had no tool to answer
with and guessed. DIDA also ships no help page — this module is the only
explain-itself surface, reached through the assistant's `explain_dida` tool.

English on purpose: the backend emits English everywhere and the consumer
localises (see the i18n architecture). The assistant is told which language to
answer in and translates these facts as it answers.

Keep entries SHORT and factual — every one is prompt tokens. Anything derivable
from code is generated (see `capabilities`), never restated here, so it cannot go
stale; only genuine domain knowledge is written by hand.
"""

from __future__ import annotations

from dida_core import command_vocabulary, readable_capabilities

# topic -> (one-line summary for the index, full text)
_TOPICS: dict[str, tuple[str, str]] = {
    "overview": (
        "What DIDA is and what each page does",
        """DIDA runs the house: devices arrive through isolated adapters, and the core
only ever knows canonical capabilities (on_off, brightness, temperature…), never a
concrete protocol. Its sibling BABA is the camera/vision system; the two share one bus.

Pages:
- Entry — a minimal page (gate + lock) for people who should not get the whole house.
- Floor plan — the main surface. Devices sit on the plan of each floor; this is where
  day-to-day control happens.
- Cameras — live camera views.
- Media — spaces, sources, and the OPUS shelf and radio played into the house.
- Heating — room targets, schedules and the boiler.
- Assistant — this chat.
- Automations — the rules. Schedules live under it.
- Energy — history and energy charts (the page is labelled Energy; it is the history
  surface).
- Settings — grouped: Devices (devices, adapters, helpers, scenes), Space (floor plans,
  areas, zones), Users, Data (retention, backup, commands), System (health, alerts,
  network, wall panel, keys, translations). Most of Settings is admin-only.""",
    ),
    "automations": (
        "How rules work: triggers, conditions, actions, and the Starlark layer",
        """A rule is Trigger → Conditions → Actions.
- Trigger: one entity + capability, plus optionally the value it changes TO and a
  `for_seconds` hold (fires only after the value has held that long). Triggers fire on
  a CHANGE of value, not on every report from the device.
- Conditions: a list; all must hold (AND). Empty means unconditional.
- Actions: run in order. Each may carry a delay that happens BEFORE it, which is how
  sequences and pulses are built (a gate relay is click–wait–click).

Layer 2 is Starlark: a sandboxed script for logic a typed rule cannot express
(branching, reading several sensors). It has no I/O and no unbounded loops, so a rule
cannot hang or crash the core. Every script is compiled and dry-run before it is saved.

In the editor a rule can be drafted from a plain-language description, and an existing
rule can be explained — including whether it would fire right now, or which condition
is currently blocking it. Rules can be enabled/disabled and force-run (force-run skips
the trigger and conditions and really drives the devices).""",
    ),
    "helpers": (
        "Virtual entities: values automations build on",
        """Helpers are entities with no hardware behind them.
- Manual: a value you set by hand — a flag, a number, a time, a selection — for rules
  to read (for example "quiet hours").
- Computed: a value defined by a rule over other statuses. A computed helper can only
  produce a value, never perform an action, so it cannot feed back into itself and
  cause a loop. Automations then use it in triggers, conditions and actions.

Helpers are the intended way to express house-wide state (day/night, quiet hours) once
instead of repeating the same condition in every rule.""",
    ),
    "schedules": (
        "Recurring calendar schedules (bin collection, irrigation season)",
        """A schedule is a recurrence — daily, weekly, monthly by date or by weekday
("the third Thursday"), or yearly — with an optional day offset so it can mark the day
BEFORE the event, which is what an announcement usually wants.

Each schedule appears as an entity carrying two readings: `schedule_active` (is today one
of its days) and `next_occurrence` (the date of the next one). Automations trigger on the
first; the second is what answers "when is the next collection". Schedules are edited
under Automations.""",
    ),
    "scenes": (
        "Captured device states, recalled as one action",
        """A scene is a snapshot of chosen entities' current values that can be recalled
later. Recall turns each captured value back into the command that sets it, so only
capabilities that actually have a settable value take part — momentary verbs (press,
play, say) are not captured.""",
    ),
    "space": (
        "Floor plans, areas and zones — and how they differ",
        """- Floor plans: the drawn plan of each floor; devices are placed on it.
- Areas: rooms. An entity belongs to one area, which is what "the kitchen light" or
  "everything downstairs" resolves against.
- Zones: geographic geofences for presence (home, work, school…), not rooms. A zone is
  matched against a person's GPS position; it has nothing to do with the floor plan.""",
    ),
    "devices": (
        "Entities, device types and what gets shown",
        """An adapter publishes entities; each entity exposes a set of capabilities. The
device type (light, switch, cover, lock, media, remote, presence, sensor, button) is
DIDA's own classification — the adapter only seeds it once, and after that the value
set in Settings wins and is never overwritten.

Per entity you can control which fields are exposed, hide it, and mark it for the voice
assistant. A hidden entity is hidden everywhere for the users it is hidden from —
including this assistant, which will not name it or act on it.""",
    ),
    "adapters": (
        "How protocols get in, and what to do when one is unhealthy",
        """One adapter per protocol, each in its own process. If an adapter dies its
entities go unavailable and everything else keeps running; the runtime restarts it.
Adapters are configured in Settings → Adapters, where each one reports a status; secrets
entered there are stored encrypted and never shown back.

DIDA does not reimplement protocols — it consumes bridges (zigbee2mqtt, ESPHome, Matter
and so on).""",
    ),
    "users": (
        "Roles, page visibility and control grants",
        """Two roles: admin and user. Admin-only surfaces include Automations and most of
Settings.

For a non-admin, an admin picks which pages they may see, and whether they may control
devices at all. That baseline can be overridden per entity with a control grant. View
rules can hide individual entities from a user.

Users are login accounts. They are not the same thing as the people tracked for
presence.""",
    ),
    "history": (
        "Where past values and past commands live",
        """Current values live in Postgres; the full history of readings and events goes to
ClickHouse, which is what the Energy page charts.

Every command is recorded with its source, so it is always answerable who or what acted:
a user, an automation, this assistant, or a voice assistant. That log is in
Settings → Data → Commands — and it is what answers "why did this turn on", which no
amount of current state can.

Rules keep their own record too: whether each firing succeeded, and the error if it did
not.""",
    ),
    "presence": (
        "Who is home, and where people are",
        """Each tracked person is an entity named presence:<name>. Its `location` reading
is the name of the zone they are in, or "away" when they are in none; some also report
battery and raw coordinates.

Zones are geofences (home, work, school), not rooms — a zone is matched against a
phone's GPS position and has nothing to do with the floor plan. People here are not the
same as login users: a person is someone the house tracks, a user is an account that
can sign in.""",
    ),
    "media": (
        "The Media page: spaces, sources, the OPUS shelf and radio",
        """One page for everything that makes sound. A space picker on top (the rooms
with a configured source), a source picker under it, and a body that changes to
whatever is playing.

- Players are ordinary entities with the media capabilities (transport, volume, source),
  so an automation or this assistant can drive them like any other device.
- The music itself is not DIDA's: the records and the radio stations live in
  OPUS · Player, which this page asks on the spot. Picking a record or a station
  puts it on the space's player; the device fetches the sound from OPUS directly.
- `radio:tuner` is a synthetic entity automations and remotes use to cycle the
  stations (next/previous) or pin one by name on the configured player.
- `opus:tv` is the OPUS app on the living-room television, as a player: what it is
  playing (a film, an episode, a song or a station), its keys, and records or
  stations put on it. It takes orders only while OPUS is open on the television.
- `opus:dac` is the DAC hanging off the OPUS server, whose sound goes to the
  amplifier's MUSIC input — the house's music and its radio (`radio:tuner` plays
  there). Its quality badge opens the signal path: the file as the library
  measured it against the format the DAC is fed.
- Speaking to the house is separate from playing to it: an `announce:<speaker>` entity
  takes a `say` command with the text, and interrupts nothing permanently.""",
    ),
    "heating": (
        "The Heating page: room targets, schedules and the boiler",
        """Heating is a control loop, not a rule: a room's TRV gets a setpoint, and the
boiler relay — a plain thermostat contact — fires while at least one room is short of
its target.

- Each room has a temperature sensor, its valves, four setpoints (comfort / eco / night
  / away) and a weekly schedule that says which one is in force when. Without a
  schedule the room holds comfort.
- The house mode overrides every room at once; `auto` lets each room follow its own
  schedule. An empty house (the configured helper) puts everything on away.
- ± on a room card holds it at a temperature for an hour, then it returns to schedule.
- The boiler has a minimum burn and a minimum rest so it cannot short-cycle, a room may
  be barred from starting it at all, and the house can require several rooms to be
  calling before it fires.
- The outdoor sensor does three things: it stops the season above the summer cutoff, it
  starts the morning climb early enough that the room ARRIVES at the scheduled
  temperature rather than beginning to heat then, and it lifts the frost floor when it
  turns properly cold.
- An open window pauses the room: a contact sensor if it has one, otherwise DIDA's own
  reading of the room's temperature falling faster than heating can explain.
- A room is not heated on a reading that stopped arriving, an open window pauses it, and
  a valve that never echoes the setpoint we wrote is reported as faulty rather than
  quietly ignored.
- Nothing is driven at all until heating is switched on — the master toggle is what makes
  it safe to configure the house before the season starts.""",
    ),
    "cameras": (
        "Cameras, and how BABA relates to DIDA",
        """Cameras belong to BABA, DIDA's sibling — the vision system. DIDA subscribes to
what BABA sees and turns it into ordinary entities: motion, occupancy, a person or
vehicle count, a recognised person. Rules use those like any other reading.

Every stream is proxied through DIDA's own API by entity id, so a browser only ever
talks to DIDA: the video rides the same login and the same tunnel as the rest of the
app, and no camera is exposed publicly. The Cameras page shows the live grid; a
notification triggered by a camera can carry the frame that caused it.

More than one BABA installation can feed one DIDA (a second house, a holiday place);
each keeps its own cameras and credentials.""",
    ),
    "notifications": (
        "Push to phones, and the companion app",
        """A rule sends a `notify` command to `notify:<person>` (or `notify:all`); the
adapter decides how it actually reaches the phone — web push to the DIDA app, or an
ntfy topic. The rule does not know which, and does not need to.

Web push needs the app installed and permission granted once per device, under Account.
The Android companion app is the same interface plus background GPS, which is what feeds
presence and geofences; it updates itself.

A notification is not the only way to be told: `announce` speaks the message on a
speaker instead, which is usually what a rule should do during the day and never during
quiet hours.""",
    ),
    "entry": (
        "The Entry page — the least-privilege surface",
        """A deliberately minimal page carrying the gate and the door lock, and nothing
else. It exists so someone who should not get the whole house — a guest, a neighbour
watering the plants — can still be let in.

It is a normal page in the permission model: give a user Entry and nothing else, and
that is the entire app for them.""",
    ),
    "system": (
        "Alerts, health, backup, retention and the rest of Settings → System/Data",
        """- Health shows every service and adapter, with the reason next to anything that
  is not healthy. An adapter reports its own status, so a bad credential says so instead
  of looking merely quiet.
- Alerts are thresholds on the house itself (an adapter down, a battery flat, a sensor
  gone silent); a firing alert is visible in the app and can notify.
- Backup makes a full database dump on a schedule and on demand, and restores one.
- Retention decides how long each class of history is kept in ClickHouse — raw readings
  are dense and short-lived, the daily rollups they feed are kept far longer.
- Commands is the audit trail: who or what sent every command.
- Network, Cloudflare and Wall panel are host-level: which interface the house's own
  devices are reached on, which routes are published to the internet, and the tablet
  that shows the panel view.
- Translations edits the display names adapters supply, without touching the device.""",
    ),
    "voice": (
        "Talking to the house through Google/Alexa/Siri",
        """DIDA exposes entities over Matter, as a bridge: a voice assistant sees lights,
plugs, covers and thermostats and can drive them, while DIDA stays the system of record.

What is exposed follows a rule — a controllable light, switch or cover is exposed on its
own, so a new device shows up in Google Home without anyone curating it. Settings →
Devices carries a per-entity override for the exceptions (a raw pulse relay is hidden;
the sequenced helper that drives it properly is exposed).

An AV activity (watch a film, play a console) is exposed as a plug rather than a light,
deliberately — "turn off the lights" must not tear down what you are watching.

Voice commands arrive as ordinary commands and are recorded in the audit trail like any
other source.""",
    ),
    "assistant": (
        "What this assistant can and cannot do, and what runs it",
        """It runs on Anthropic's Claude — Sonnet for the conversation and the tools,
and Opus for the harder job of turning a description into an automation. The house sends
it the devices and readings it asks for; it holds no memory between conversations.

This assistant drives the same validated path as the rest of the app: commands
are checked against the capability model and published to the bus, and rules are checked
before they are stored. It cannot exceed the permissions of the person talking to it —
entities hidden from you stay invisible, control still needs your grant, and only an
admin can create automations.

It can: list devices, read current values, send commands, set helpers (they are ordinary
entities), say who is home, query history, report firing system alerts, create a rule
from a description, list/enable/disable/run existing rules, explain a rule and diagnose
why it is or is not firing, show when rules fired and which failed, show who commanded
a device and when, list and recall scenes, and explain how DIDA works. The rule tools
are admin-only, as they are everywhere else.

It cannot: create or edit scenes, add or delete helpers, place devices on the floor plan,
configure adapters, or manage users — do those in the app. It has no eyes: it can say
whether a camera reports motion or a recognised person, but it cannot show you the
picture.""",
    ),
    "capabilities": (
        "The full command vocabulary, per capability",
        """The core knows only capabilities, never devices. A command is valid only if the
capability declares it, and a value is valid only inside the capability's own range or
choice set — anything else is rejected at the boundary rather than half-applied.

Writable capabilities and their exact commands:
{vocabulary}

Read-only (report values, accept no command):
{readable}

Conventions that are not visible in the list above:
- announce: the entity is `announce:<speaker>` and the command is `say`, whose value is
  the text to speak.
- media_transport: `play_media` carries a uri (plus optional title/art), not a value.
- Both `<x>` and `<x>_options` may be present: the plain one is the current value and
  the `_options` one carries the list of values `set_<x>` will accept.""",
    ),
}


def help_index() -> str:
    """Topic list, for when the model has not named one."""
    lines = [f"- {name}: {summary}" for name, (summary, _) in _TOPICS.items()]
    return "DIDA help topics (call explain_dida again with one of these):\n" + "\n".join(lines)


def help_text(topic: str | None = None) -> str:
    """One topic's text, or the index when the topic is missing or unknown.

    Unknown topics return the index rather than an error: the model picked a word,
    and showing it the real list is more useful than making it guess again.
    """
    if not topic:
        return help_index()
    key = topic.strip().lower().replace(" ", "_")
    entry = _TOPICS.get(key)
    if entry is None:
        # Cheap synonym pass before giving up — "rules" should not dead-end.
        for name, (summary, _) in _TOPICS.items():
            if key in name or key in summary.lower():
                entry = _TOPICS[name]
                break
    if entry is None:
        return f"No topic named {topic!r}.\n\n" + help_index()
    text = entry[1]
    if "{vocabulary}" in text:
        text = text.format(vocabulary=command_vocabulary(), readable=readable_capabilities())
    return text


HELP_TOPIC_NAMES: tuple[str, ...] = tuple(_TOPICS)
