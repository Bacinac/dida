import { registerModule, type Word } from "$lib/kit";
import { hr } from "./hr";
import { en } from "./en";

export type MessageKey = keyof typeof hr | Word;

export const t = registerModule({ hr, en });
