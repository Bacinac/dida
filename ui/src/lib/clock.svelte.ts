// One shared minute hand. A surface showing an elapsed time ("parked for 2 h
// 10 min") reads `clock.now`, so the number ages on its own — one timer for the
// whole app instead of one per marker, and every such reading ticks together.
class Clock {
  now = $state(Date.now());
}

export const clock = new Clock();

if (typeof window !== "undefined") {
  setInterval(() => { clock.now = Date.now(); }, 30_000);
}
