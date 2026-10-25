Heating is a control loop, not a rule. Each room's radiator valve is opened and shut by the room's own thermometer, and the boiler relay — a plain thermostat contact — fires while at least one room allowed to fire it is short of its target.

## Rooms

- Each room has a temperature sensor, its valves, four setpoints (comfort, eco, night, away) and a weekly schedule that says which one is in force when. Without a schedule the room holds comfort.
- **±** on a room card holds it at a temperature for an hour; then it returns to its schedule.
- An open window pauses the room: from a contact sensor if it has one, otherwise from DIDA's own reading of the temperature falling faster than heating can explain.
- **Eco while nobody is in** (a room with an occupancy or motion sensor): once every sensor in the room has seen no one for the configured minutes, comfort runs as eco; the first person seen brings comfort back. A sensor that is unreachable or has never reported keeps the room on comfort. The morning climb of an empty room aims at eco too.
- In a room with its own thermometer the valves only open and shut: while the room calls for heat DIDA sets them to 28 °C, otherwise to 10 °C (or the valve's own limits). The valve's own thermometer sits on the radiator and is not used. At 10 °C a valve still opens by itself against frost. A room without a thermometer, a room that is off, an open window and the freeze guard get the room's own target, and the valve regulates by itself.

## The house

- The **house mode** overrides every room at once; `auto` lets each room follow its own schedule. An empty house (the configured helper) puts everything on away once it has stayed empty for the same minutes as an empty room, so one stray phone location does not cool the house.
- The **outdoor sensor** does three things: it stops the season above the summer cutoff; it starts the morning climb early enough that the room ARRIVES at the scheduled temperature rather than starting to heat then; and it raises the frost floor when it turns properly cold.

## The boiler

- A minimum burn and a minimum rest keep it from short-cycling.
- A room may be barred from firing it: it then takes heat only while the boiler runs for others and never keeps it burning. The house can require several rooms to be calling before it fires.

## What it refuses to do

- A room is not heated on a reading that stopped arriving.
- A valve that never echoes the setpoint written to it is reported as faulty rather than quietly ignored.
- The boiler is stopped for shut valves only when every valve has been seen open at some point and now reports shut; a valve that always reports 0 % proves nothing.
- Nothing is driven at all until heating is switched on. The master switch is what makes it safe to set the house up before the season starts.
