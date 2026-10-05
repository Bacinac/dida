#!/usr/bin/env node
// Every word the UI says, checked rather than remembered — by the kit's checker,
// with the families only DIDA knows. Most of them are vocabularies the backend
// owns, so it reads the repository, not just ui/: `tests/run.sh`.

import { join } from 'node:path';
import { readFileSync } from 'node:fs';
import { checkWords, quotedIn, report } from '../kit/words/check.mjs';

// This file sits at <root>/ui/src/lib/i18n/.
const ROOT = join(new URL('.', import.meta.url).pathname, '../../../..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const all = (path, re) => [...read(path).matchAll(re)].map((m) => m[1]);
const block = (path, re) => {
	const found = re.exec(read(path));
	return found ? found[1] : '';
};
const range = (n) => Array.from({ length: n }, (_, i) => String(i));

const CAPS = 'core/src/dida_core/capabilities.py';
const UI_CAPS = 'ui/src/lib/capabilities.ts';
const choices = (kind) => () => quotedIn(ROOT, CAPS, new RegExp(`CapabilityKind\\.${kind}: CapabilitySpec\\([\\s\\S]*?choices=\\(([^)]*)\\)`));
const HEATING = 'ui/src/lib/heating.ts';
const ADAPTERS_PAGE = 'ui/src/routes/settings/adapters/+page.svelte';
const DEVICES_PAGE = 'ui/src/routes/settings/devices/+page.svelte';

const families = {
	cap: {
		where: `CapabilityKind in ${CAPS}, and BADGE_CAPS' on/off in ${UI_CAPS}`,
		members: () => [
			...[...block(CAPS, /class CapabilityKind\(StrEnum\):([\s\S]*?)\nclass /).matchAll(/^ {4}[A-Z_0-9]+ = "(\w+)"/gm)].map((m) => m[1]),
			...quotedIn(ROOT, UI_CAPS, /BADGE_CAPS = new Set<string>\(\[([^\]]*)\]/).flatMap((c) => [`${c}.on`, `${c}.off`])
		]
	},
	cmd: {
		where: `COMMANDS in ${UI_CAPS}`,
		members: () => [...block(UI_CAPS, /const COMMANDS[^=]*= \{([\s\S]*?)\n\};/).matchAll(/command: "(\w+)"/g)].map((m) => m[1])
	},
	devtype: {
		where: `DEVICE_TYPES in ${CAPS}`,
		members: () => quotedIn(ROOT, CAPS, /DEVICE_TYPES: frozenset\[str\] = frozenset\(\s*\{([^}]*)\}/)
	},
	objclass: { where: `the OBJECT_CLASS choices in ${CAPS}`, members: choices('OBJECT_CLASS') },
	identity: { where: `the IDENTITY_PRESENCE choices in ${CAPS}`, members: choices('IDENTITY_PRESENCE') },
	// the vendor's fault texts, which it keeps adding to
	mowererr: { open: true },
	'detail.event': {
		where: 'JournalKind in core/src/dida_core/journal.py',
		members: () => quotedIn(ROOT, 'core/src/dida_core/journal.py', /JournalKind = Literal\[([^\]]*)\]/)
	},
	'alerts.rule': {
		where: 'the rules services/api/src/dida_api/alerts.py evaluates',
		members: () => all('services/api/src/dida_api/alerts.py', /"(\w+)" in rules/g)
	},
	'system.orphanKind': {
		where: 'WALKED_COLUMNS in core/src/dida_core/references.py and the reports in services/api/src/dida_api/orphans.py',
		members: () => [
			...[...block('core/src/dida_core/references.py', /WALKED_COLUMNS = \(([\s\S]*?)\n\)/).matchAll(/\("(\w+)",/g)].map((m) => m[1]),
			...all('services/api/src/dida_api/orphans.py', /report\("(\w+)"/g)
		]
	},
	role: {
		where: 'the roles services/api/src/dida_api/users.py accepts',
		members: () => quotedIn(ROOT, 'services/api/src/dida_api/users.py', /role not in \(([^)]*)\)/)
	},
	'users.scope': {
		where: 'CONTROL_SCOPES in services/api/src/dida_api/users.py',
		members: () => quotedIn(ROOT, 'services/api/src/dida_api/users.py', /CONTROL_SCOPES = frozenset\(\(([^)]*)\)\)/)
	},
	'energy.role': {
		where: 'ROLES in services/api/src/dida_api/energy.py',
		members: () => quotedIn(ROOT, 'services/api/src/dida_api/energy.py', /^ROLES = \(([^)]*)\)/m)
	},
	'entry.slot': {
		where: 'ACTION_SLOTS in services/api/src/dida_api/entry.py',
		members: () => quotedIn(ROOT, 'services/api/src/dida_api/entry.py', /ACTION_SLOTS = \(([^)]*)\)/)
	},
	'cf.serve': {
		where: 'SERVE_VALUES in adapters/cloudflare/src/dida_adapter_cloudflare/adapter.py',
		members: () => quotedIn(ROOT, 'adapters/cloudflare/src/dida_adapter_cloudflare/adapter.py', /SERVE_VALUES = \(([^)]*)\)/)
	},
	'esphome.err': {
		where: '_classify_error in adapters/esphome/src/dida_adapter_esphome/adapter.py',
		members: () => [
			...block('adapters/esphome/src/dida_adapter_esphome/adapter.py', /def _classify_error([\s\S]*?)\n\n/).matchAll(/return "(\w+)"/g)
		].map((m) => m[1])
	},
	'heating.status': {
		where: 'the ST_ constants in services/automation/src/dida_automation/heating.py',
		members: () => all('services/automation/src/dida_automation/heating.py', /^ST_\w+ = "(\w+)"/gm)
	},
	'heating.profile': {
		where: `PROFILES in ${HEATING}`,
		members: () => quotedIn(ROOT, HEATING, /export const PROFILES = \[([^\]]*)\]/)
	},
	'heating.mode': {
		where: `MODES in ${HEATING}`,
		members: () => [
			...quotedIn(ROOT, HEATING, /export const MODES = \[([^\]]*)\]/),
			...quotedIn(ROOT, HEATING, /export const PROFILES = \[([^\]]*)\]/),
			...quotedIn(ROOT, HEATING, /export const SCHEDULE_PROFILES = \[\.\.\.PROFILES, ([^\]]*)\]/)
		]
	},
	'day.short': { where: 'the weekdays of Date.getDay()', members: () => range(7) },
	'schedule.month': { where: 'the months of Date.getMonth()', members: () => range(12) },
	'schedule.wd': { where: 'the weekdays of Date.getDay()', members: () => range(7) },
	'schedule.wdGen': { where: 'the weekdays of Date.getDay()', members: () => range(7) },
	'schedule.wdFull': { where: 'the weekdays of Date.getDay()', members: () => range(7) },
	'schedule.recur': {
		where: 'RECURRENCES in core/src/dida_core/schedules.py',
		members: () => quotedIn(ROOT, 'core/src/dida_core/schedules.py', /RECURRENCES = \{([^}]*)\}/)
	},
	'schedule.unit': {
		where: 'RECURRENCES in core/src/dida_core/schedules.py',
		members: () => quotedIn(ROOT, 'core/src/dida_core/schedules.py', /RECURRENCES = \{([^}]*)\}/)
	},
	'room.kind': {
		where: 'KINDS in ui/src/routes/settings/areas/+page.svelte',
		members: () => quotedIn(ROOT, 'ui/src/routes/settings/areas/+page.svelte', /const KINDS = \[([^\]]*)\]/)
	},
	// OPUS's places and what is in them are OPUS's to name
	'opus.place': { open: true },
	'media.opusItem': { open: true },
	media: {
		where: 'Transport in ui/src/lib/media.ts',
		members: () => quotedIn(ROOT, 'ui/src/lib/media.ts', /export type Transport = ([^;]*);/)
	},
	theme: {
		where: 'Theme in ui/src/lib/kit/theme.svelte.ts',
		members: () => quotedIn(ROOT, 'ui/src/lib/kit/theme.svelte.ts', /export type Theme = ([^;]*);/)
	},
	language: {
		where: 'Locale in ui/src/lib/kit/i18n.svelte.ts',
		members: () => quotedIn(ROOT, 'ui/src/lib/kit/i18n.svelte.ts', /export type Locale = ([^;]*);/)
	},
	'presence.share': {
		where: 'GeoStatus in ui/src/lib/geolocation.svelte.ts',
		members: () => quotedIn(ROOT, 'ui/src/lib/geolocation.svelte.ts', /export type GeoStatus = ([^;]*);/)
	},
	'push.status': {
		where: 'PushStatus in ui/src/lib/push.svelte.ts',
		members: () => quotedIn(ROOT, 'ui/src/lib/push.svelte.ts', /export type PushStatus =([^;]*);/)
	},
	fields: {
		where: `the entity groups in ${ADAPTERS_PAGE}`,
		members: () => quotedIn(ROOT, ADAPTERS_PAGE, /return \((\["controls"[^\]]*\]) as const\)/)
	},
	'devices.elsewhere': {
		where: `ELSEWHERE in ${DEVICES_PAGE}`,
		members: () => quotedIn(ROOT, DEVICES_PAGE, /const ELSEWHERE = \[([^\]]*)\] as const/)
	}
};

report(checkWords({ root: ROOT, src: 'ui/src', packages: ['ui/src/lib/kit'], families, leftovers: true }));
