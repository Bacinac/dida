/** Unwrap a thrown value to a human string for display.
 *
 * Replaces the `e instanceof Error ? e.message : String(e)` idiom that was
 * inlined in ~65 catch blocks across the app. */
export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
