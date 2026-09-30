// Auth state. One /auth/me probe on load decides authenticated-or-not; the
// layout guard reacts to `user`. Cookie is HttpOnly so JS never sees the token.

import { api, Unauthorized, type Me } from "$lib/api";
import { i18n, theme, type Locale, type Theme } from "$lib/kit";

class AuthStore {
  user = $state<Me | null>(null);
  checked = $state(false); // initial /me probe done?

  /** Admins configure (settings, rooms, automations, users); users only operate. */
  get isAdmin(): boolean {
    return this.user?.role === "admin";
  }

  /** Whether the current user may see a given page key. Admins see everything;
   * a null `allowed_pages` means full consumer access (the default). Enforced
   * in the layout nav filter + route guard. Page keys: ulaz · floorplan ·
   * devices · media · assistant · history · adapters. */
  canSee(page: string): boolean {
    if (this.isAdmin) return true;
    const allowed = this.user?.allowed_pages;
    return allowed == null || allowed.includes(page);
  }

  /** Baseline device-control permission. Admins and default users may operate
   * devices; a view-only user (can_control=false) cannot. Fine-grained per-entity
   * exceptions are enforced server-side at /command — this is the coarse gate the
   * UI uses to hide control affordances from a view-only user. */
  get canControl(): boolean {
    return this.isAdmin || this.user?.can_control !== false;
  }

  async load(): Promise<void> {
    try {
      this.user = await api.me();
      this.#applyPrefs();
    } catch (e) {
      // Only a 401 means "not authenticated". A 502/500/network failure means the
      // API is momentarily unreachable — which happens on every deploy — and
      // clearing `user` there logged an open tab out and bounced it to /login even
      // though its cookie was still perfectly valid. Keep the session on anything
      // that isn't a definitive rejection.
      if (e instanceof Unauthorized) this.user = null;
    } finally {
      this.checked = true;
    }
  }

  /** Throws on bad credentials / rate limit — the login page shows the message. */
  async login(username: string, password: string): Promise<void> {
    this.user = await api.login(username, password);
    this.#applyPrefs();
  }

  /** Apply the user's saved theme/language once, right after we learn who they
   * are — the DB preference is the source of truth across devices, so it wins
   * over the device-local (localStorage) default already applied on boot. The
   * setters refresh that localStorage cache, so a matching choice is a no-op. */
  #applyPrefs(): void {
    const u = this.user;
    if (!u) return;
    if (u.theme) theme.setTheme(u.theme);
    if (u.locale) i18n.set(u.locale);
  }

  /** Persist a theme choice to the user's profile (fire-and-forget: a failed
   * write just won't follow them to another device). Called from wherever the
   * theme changes — the header switcher and the account page. */
  saveTheme(value: Theme): void {
    if (!this.user) return;
    this.user.theme = value;
    void api.setPrefs({ theme: value }).catch(() => {});
  }

  /** Persist a language choice to the user's profile. See saveTheme. */
  saveLocale(value: Locale): void {
    if (!this.user) return;
    this.user.locale = value;
    void api.setPrefs({ locale: value }).catch(() => {});
  }

  async logout(): Promise<void> {
    try {
      await api.logout();
    } finally {
      this.user = null;
    }
  }
}

export const auth = new AuthStore();
