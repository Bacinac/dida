// Which build this tab runs, and whether a newer one is served: the kit's
// watcher over DIDA's /version (revision.json, stamped by the push hook).

import { VersionWatch } from "$lib/kit";
import { api, type Revision } from "$lib/api";

export const version = new VersionWatch<Revision>(() => api.version());
