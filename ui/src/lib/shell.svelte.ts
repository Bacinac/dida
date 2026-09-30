// Cross-component chrome state (mirrors BABA's ui store) — the right home for
// any shell-level state shared between pages and the root layout.

class UiStore {
  /** A page-level focused task (floorplan edit) is active — its own toolbox
   *  takes the bottom of a small screen, so the mobile tab bar hides. */
  focusMode = $state(false);

  /** The assistant drawer. It is a surface over whatever page you are on, not a
   *  destination: asking "which lights are on" should not cost you the floor
   *  plan you were looking at, and on a phone it should not spend one of the
   *  four tab slots. Visibility is still gated by the `assistant` page key. */
  assistantOpen = $state(false);
  toggleAssistant() {
    this.assistantOpen = !this.assistantOpen;
  }

  /** A question queued for the assistant by some other surface (the header's
   *  explain-this-page button). The drawer mounts lazily, so the question waits
   *  here until the panel is alive to send it, and is cleared on the way out. */
  pendingQuestion = $state<string | null>(null);
  askAssistant(question: string) {
    this.pendingQuestion = question;
    this.assistantOpen = true;
  }
}

export const ui = new UiStore();
