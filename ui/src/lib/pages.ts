// Single source of truth for the consumer pages whose per-user visibility an
// admin can scope (settings/users → allowed_pages). EVERY frontend surface that
// needs the page list derives from CONSUMER_PAGES — the sidebar nav + phone tab
// bar + route guard (+layout.svelte) and the users admin checkboxes
// (settings/users/+page.svelte). No surface keeps its own copy.
//
// The canonical ORDER here mirrors PAGE_ORDER in services/api/src/dida_api/users.py,
// which is the server-side authority: it validates allowed_pages and stores the
// set in this order. Adding/removing a page = edit this array AND that tuple
// (two runtimes, one list — codegen isn't worth it for a handful of pages).

import type { MessageKey } from "$lib/i18n";

export type ConsumerPage = {
  /** Stable id persisted in users.allowed_pages; matches a PAGE_ORDER entry. */
  key: string;
  /** Landing route the page lives at. Absent for an `overlay` surface, which has
   *  no route to navigate to — nothing may link to it or bounce a user there. */
  href?: string;
  /** This key gates a surface that opens OVER the current page instead of being
   *  a destination (the assistant drawer). It keeps its entry here because the
   *  per-user permission is the same one — only the presentation differs. */
  overlay?: true;
  /** i18n key for the sidebar / tab / checkbox label. */
  nav: MessageKey;
  /** Inline stroke-SVG icon for this page's phone bottom-bar tab. Required for
   *  every key in PHONE_TAB_ORDER; omitted for pages that never get a tab
   *  (history, adapters — they live only in the "More" sheet, as text links). */
  icon?: string;
};

export const CONSUMER_PAGES: readonly ConsumerPage[] = [
  { key: "entry", href: "/entry", nav: "nav.entry",
    icon: '<path d="M18 20V6a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v14"/><path d="M2 20h20"/><path d="M14 12v.01"/>' },
  { key: "floorplan", href: "/floorplan", nav: "nav.floorplan",
    icon: '<rect x="3" y="3" width="18" height="18" rx="1"/><path d="M3 12h8V3M11 12v4M21 15h-5v6"/>' },
  { key: "devices", href: "/settings/devices", nav: "nav.devices",
    icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>' },
  { key: "cameras", href: "/cameras", nav: "nav.cameras",
    icon: '<path d="m22 8-6 4 6 4V8Z"/><rect x="2" y="6" width="14" height="12" rx="2"/>' },
  { key: "media", href: "/media", nav: "nav.media",
    icon: '<circle cx="7" cy="18" r="3"/><circle cx="17" cy="16" r="3"/><path d="M10 18V5l10-2v13"/>' },
  { key: "heating", href: "/heating", nav: "nav.heating",
    icon: '<path d="M12 3v10"/><circle cx="12" cy="17" r="4"/><path d="M9 8h3M9 5h3"/>' },
  { key: "assistant", overlay: true, nav: "nav.assistant",
    icon: '<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.6-.8L3 21l1.8-5.9A8.5 8.5 0 1 1 21 11.5z"/>' },
  // history normally lives in the "More" sheet (not in PHONE_TAB_ORDER), but it
  // still carries an icon: when it is the ONLY sheet entry (restricted family
  // member), the layout promotes it to a direct 5th tab instead of a one-item
  // "More" sheet.
  { key: "history", href: "/history", nav: "nav.history",
    icon: '<path d="M13 2 3 14h7l-1 8 11-14h-7l1-6z"/>' },
  { key: "adapters", href: "/settings/adapters", nav: "nav.adapters" },
];

// Phone bottom-bar tabs, in thumb-priority order — deliberately NOT the canonical
// order above (floorplan is the daily main surface; entry/cameras are occasional).
// The bar shows the first MAX_TABS of these the user can see; every page past the
// cap, plus every non-tab page (history, automations, settings…), falls to the
// "More" sheet. Keys must be CONSUMER_PAGES entries that carry an `icon`.
// The assistant is deliberately absent: it is a drawer reachable from the header
// on every page, so spending one of four thumb slots on it would buy nothing and
// cost a real destination.
export const PHONE_TAB_ORDER: readonly string[] = [
  "floorplan", "media", "cameras", "entry",
];
