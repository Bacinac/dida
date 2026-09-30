A schedule is a recurrence: daily, weekly, monthly by date or by weekday ("the third Thursday"), or yearly. An optional day offset lets it mark the day BEFORE the event, which is usually what an announcement wants — the bins go out the evening before.

Each schedule appears as an entity with two readings:

- `schedule_active` — whether today is one of its days. [Automations](/help/automations) trigger on this.
- `next_occurrence` — the date of the next one. This is what answers "when is the next collection".

Schedules are edited under Automations.
