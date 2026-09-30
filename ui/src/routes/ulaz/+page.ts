import { redirect } from "@sveltejs/kit";

// The old Croatian path, kept only as a redirect: phones in the family have it
// bookmarked, and a bookmark must not 404 because routes went English-only.
export const load = () => {
  redirect(301, "/entry");
};
