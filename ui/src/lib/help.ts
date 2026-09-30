// "Explain this page" — the app's only help affordance, and deliberately not a
// help SECTION. DIDA already has one surface that explains itself (the assistant's
// explain_dida tool, backed by services/api/.../help_docs.py); a second, hand-written
// docs tree would be a copy to keep in sync, and copies rot. So the header button
// asks the assistant the question the user would have typed, naming the page they
// are actually on.
//
// The labels are NOT a new list: they come from the same CONSUMER_PAGES and
// SETTINGS_GROUPS the nav is built from. Only the routes that live outside both
// (automations, its schedules tab, account, about) are named here.

import type { MessageKey } from "$lib/i18n";
import { CONSUMER_PAGES } from "$lib/pages";
import { SETTINGS_GROUPS } from "$lib/settingsNav";

const EXTRA: readonly { href: string; key: MessageKey }[] = [
  { href: "/automations/schedules", key: "nav.schedules" },
  { href: "/automations", key: "nav.automations" },
  { href: "/settings", key: "nav.settings" },
  { href: "/account", key: "account.title" },
];

/** Every known route with the i18n key of its name, longest href first so
 *  /automations/schedules wins over /automations. */
const ROUTES: readonly { href: string; key: MessageKey }[] = [
  ...CONSUMER_PAGES.filter((p) => p.href).map((p) => ({ href: p.href as string, key: p.nav })),
  ...SETTINGS_GROUPS.flatMap((g) => g.tabs.map((tab) => ({ href: tab.href, key: tab.key }))),
  ...EXTRA,
].sort((a, b) => b.href.length - a.href.length);

/** The i18n key naming the page at `pathname`, or null for a route with no name
 *  of its own (login, onboarding, the wall panel) — the button hides there. */
export function pageNameKey(pathname: string): MessageKey | null {
  const hit = ROUTES.find((r) => pathname === r.href || pathname.startsWith(r.href + "/"));
  return hit?.key ?? null;
}
