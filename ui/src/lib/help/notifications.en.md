A [rule](/help/automations) sends a `notify` command to `notify:<person>` (or `notify:all`). The adapter decides how it actually reaches the phone — web push to the DIDA app, or an ntfy topic. The rule does not know which, and does not need to.

- **Web push** needs the app installed and permission granted once per device, under **Account**.
- The **Android companion app** is the same interface plus background GPS, which is what feeds [presence](/help/presence) and the geofences. It updates itself.

A notification is not the only way to be told. `announce` speaks the message on a speaker instead ([Media](/help/media)) — usually what a rule should do during the day, and never during quiet hours.
