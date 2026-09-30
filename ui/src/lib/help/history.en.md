Current values live in Postgres. The full history of readings and events goes to ClickHouse, and that is what the **Energy** page charts. How long each kind of history is kept is set under [Retention](/help/system).

Every command is recorded with its source, so it is always answerable who or what acted: a user, an automation, the assistant or a voice assistant. That log is in **Settings → Data → Commands**, and it answers "why did this turn on" — a question no amount of current state can.

[Rules](/help/automations) keep their own record too: whether each firing succeeded, and the error when it did not.
