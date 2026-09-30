// The Settings area's information architecture: logical GROUPS, each rendered as a
// page with a horizontal tab bar (settings/+layout.svelte). The top sidebar shows
// the groups; a group's pages are its tabs — no more 13-link flat list.
//
// Per-tab visibility mirrors the main nav (+layout.svelte): `admin` = admin-only;
// `page` = a CONSUMER_PAGES key gated by allowed_pages (devices/adapters). A group
// is shown when ANY of its tabs is visible; its landing is the first visible tab.

import type { MessageKey } from "$lib/i18n";

export type SettingsTab = { href: string; key: MessageKey; admin?: boolean; page?: string };
export type SettingsGroup = { key: MessageKey; tabs: SettingsTab[] };

export const SETTINGS_GROUPS: readonly SettingsGroup[] = [
  {
    key: "nav.gDevices",
    tabs: [
      { href: "/settings/devices", key: "nav.devices", page: "devices" },
      { href: "/settings/adapters", key: "nav.adapters", page: "adapters" },
      { href: "/settings/helpers", key: "nav.helpers", admin: true },
      { href: "/settings/scenes", key: "nav.scenes", admin: true },
    ],
  },
  {
    key: "nav.gSpace",
    tabs: [
      { href: "/settings/floors", key: "nav.floors", admin: true },
      { href: "/settings/areas", key: "nav.rooms", admin: true },
      { href: "/settings/zones", key: "nav.zones", admin: true },
    ],
  },
  {
    key: "nav.gUsers",
    tabs: [
      { href: "/settings/users", key: "nav.users", admin: true },
      { href: "/settings/contacts", key: "nav.contacts", admin: true },
    ],
  },
  {
    key: "nav.gData",
    tabs: [
      { href: "/settings/retention", key: "nav.retention", admin: true },
      { href: "/settings/backup", key: "nav.backup", admin: true },
      { href: "/settings/commands", key: "nav.commands", admin: true },
      { href: "/settings/logs", key: "nav.logs", admin: true },
    ],
  },
  {
    key: "nav.gSystem",
    tabs: [
      { href: "/settings/system", key: "nav.health", admin: true },
      { href: "/settings/alerts", key: "nav.alerts", admin: true },
      { href: "/settings/network", key: "nav.network", admin: true },
      { href: "/settings/panel", key: "nav.panel", admin: true },
      { href: "/settings/keys", key: "nav.keys", admin: true },
      { href: "/settings/translations", key: "nav.translations", admin: true },
    ],
  },
];
