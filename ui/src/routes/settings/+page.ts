import { redirect } from "@sveltejs/kit";

// /settings has no page of its own — it opens the first group's first tab
// (Devices). A non-admin who can't see it is sent on to their first allowed page
// by the layout route guard.
export const load = () => {
  redirect(307, "/settings/devices");
};
