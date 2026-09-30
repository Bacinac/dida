// That every key is said in both languages is words.mjs's to check; these hold
// what the schedule page does with the words once it has them.
import { describe, expect, it } from "vitest";
import { hr } from "$lib/i18n/hr";
import { en } from "$lib/i18n/en";

describe("schedules vocabulary", () => {
  it("keeps the {param} placeholders the page interpolates", () => {
    for (const cat of [hr, en]) {
      expect(cat["schedule.monthlyDay"]).toContain("{day}");
      expect(cat["schedule.monthlyWeekday"]).toContain("{occ}");
      expect(cat["schedule.monthlyWeekday"]).toContain("{wd}");
    }
  });

  it("keeps Croatian genitive weekday forms distinct from the nominative", () => {
    // "svakog 2. utorka", not "svakog 2. utorak" — the inflection is the reason
    // these live in the catalog rather than being derived.
    expect(hr["schedule.wdGen.1"]).toBe("utorka");
    expect(hr["schedule.wdFull.1"]).toBe("utorak");
  });
});
