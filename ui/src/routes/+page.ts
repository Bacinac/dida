import { redirect } from "@sveltejs/kit";

// Home is the floor plan — the primary control surface. The device grid moved to
// Settings → Devices. A restricted user who can't see the floor plan is bounced on
// to their first allowed page by the layout route guard.
export const load = () => {
  redirect(307, "/floorplan");
};
