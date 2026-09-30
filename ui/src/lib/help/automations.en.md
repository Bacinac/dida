A rule is **Trigger → Conditions → Actions**.

- **Trigger** — one entity and capability, optionally the value it changes TO, and optionally a hold (`for_seconds`): the rule fires only once the value has held that long. A trigger fires on a CHANGE of value, not on every report from the device.
- **Conditions** — a list, all of which must hold (AND). No conditions means the rule is unconditional.
- **Actions** — run in order. Each may carry a delay that happens BEFORE it, which is how sequences and pulses are built: a gate relay is click, wait, click.

## When a typed rule is not enough

The second layer is **Starlark**: a sandboxed script for logic a typed rule cannot express, such as branching or reading several sensors at once. It has no I/O and no unbounded loops, so a rule cannot hang or crash the core. Every script is compiled and dry-run before it is saved.

## In the editor

- A rule can be drafted from a plain-language description.
- An existing rule can be explained — including whether it would fire right now, or which condition is blocking it.
- Rules can be enabled, disabled and force-run. Force-run skips the trigger and the conditions and really drives the devices.

Each rule keeps a record of its firings, and of the error when one failed ([History](/help/history)). Values the whole house shares, such as quiet hours, belong in a [helper](/help/helpers) rather than in a condition repeated in every rule; recurring dates belong in a [schedule](/help/schedules).
