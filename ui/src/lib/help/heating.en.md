Heating is a control loop, not a rule. Each room's radiator valve gets a setpoint, and the boiler relay — a plain thermostat contact — fires while at least one room is short of its target.

## Rooms

- Each room has a temperature sensor, its valves, four setpoints (comfort, eco, night, away) and a weekly schedule that says which one is in force when. Without a schedule the room holds comfort.
- **±** on a room card holds it at a temperature for an hour; then it returns to its schedule.
- An open window pauses the room: from a contact sensor if it has one, otherwise from DIDA's own reading of the temperature falling faster than heating can explain.

## The house

- The **house mode** overrides every room at once; `auto` lets each room follow its own schedule. An empty house (the configured helper) puts everything on away.
- The **outdoor sensor** does three things: it stops the season above the summer cutoff; it starts the morning climb early enough that the room ARRIVES at the scheduled temperature rather than starting to heat then; and it raises the frost floor when it turns properly cold.

## The boiler

- A minimum burn and a minimum rest keep it from short-cycling.
- A room may be barred from starting it at all, and the house can require several rooms to be calling before it fires.

## What it refuses to do

- A room is not heated on a reading that stopped arriving.
- A valve that never echoes the setpoint written to it is reported as faulty rather than quietly ignored.
- Nothing is driven at all until heating is switched on. The master switch is what makes it safe to set the house up before the season starts.
