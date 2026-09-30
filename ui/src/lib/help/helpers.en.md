Helpers are entities with no hardware behind them.

- **Manual** — a value you set by hand: a flag, a number, a time, a selection. Rules read it — "quiet hours" is the usual example.
- **Computed** — a value defined by a rule over other readings. A computed helper can only produce a value, never perform an action, so it cannot feed back into itself and cause a loop.

Automations then use helpers in triggers, conditions and actions like any other entity, and the assistant can set a manual one.

Helpers are the intended way to say house-wide state — day or night, quiet hours, someone is home — once, instead of repeating the same condition in every [rule](/help/automations).
