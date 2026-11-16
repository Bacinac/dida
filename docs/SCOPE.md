# DIDA — scope

Progress and plans live on the plan board in Ponistra (Plan DIDA); this file holds what is
deliberately not planned.

## Options

Not planned, possibly never needed; each enters the plan as a step only when its trigger occurs.

- **HOME.TV with one click at the holiday house.** Needs a permanent tunnel to the holiday house's ADB or a local
  DIDA there; meanwhile the tile is opened with the remote. Trigger: either of those exists.
- **SmartThings becoming paid.** Samsung announced a paid API for individuals (2026-10-13).
  Trigger: the access answers 401/403; reconnecting will not help, so a subscription or a
  replacement for the washing machine.
- **`calendar:workday` (weekend + public holidays).** Ponistra's non-working days do not
  leave it over CalDAV, so a workday entity would need a holiday list of its own.
  Trigger: the first automation that needs to tell a working day from a day off.
- **Away from a Google calendar.** `calendar:away` reads Ponistra only; the contacts
  grant already carries `calendar.readonly`. Trigger: an installation that wants the
  away calendar without Ponistra.
