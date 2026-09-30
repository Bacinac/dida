// Display-name translations — localises DYNAMIC adapter-generated descriptors
// (entity facet names + device-card headers). Adapters emit a stable ENGLISH
// descriptor; this maps it to the active language, falling back to the English
// key. Fixed UI chrome stays in the static i18n catalogs (t()); only data-driven
// descriptors come through here, so a new adapter needs no frontend change.
//
// Identity is always entity_id/device_key — this is a pure display dictionary
// keyed on the source string, so translating never affects a reference.

import { api } from "$lib/api";
import { i18n } from "$lib/kit";

class Translations {
  // key (English descriptor) -> localised value, for the ACTIVE language.
  map = $state<Record<string, string>>({});

  async load(): Promise<void> {
    // 'en' is the identity base (the keys ARE the English), so there is nothing
    // to fetch — tr() then returns the key unchanged.
    if (i18n.locale === "en") {
      this.map = {};
      return;
    }
    try {
      this.map = await api.getTranslations(i18n.locale);
    } catch {
      this.map = {}; // fall back to the English keys on any error
    }
  }

  /** Localise a descriptor: its translation for the active language, else itself
   *  (device/user names have no translation and pass through unchanged). */
  tr(s: string): string {
    return this.map[s] ?? s;
  }
}

export const translations = new Translations();
export const tr = (s: string): string => translations.tr(s);
