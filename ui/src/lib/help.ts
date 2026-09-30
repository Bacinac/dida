// DIDA's articles (help/index.json and a body per language), for the frame's
// "?", the /help pages and every Hint. The API image carries the same
// directory: the assistant's explain_dida reads the English bodies.
import { Help, type HelpEntry } from "$lib/kit";
import index from "./help/index.json";

export const help = new Help(
  index as HelpEntry[],
  import.meta.glob("./help/*.md", { query: "?raw", import: "default", eager: true }),
);
