<script lang="ts">
  import { goto } from "$app/navigation";
  import { Button, Notice } from "$lib/kit";
  import { page } from "$app/state";
  import { auth } from "$lib/auth.svelte";
  import { Unauthorized } from "$lib/api";
  import { t } from "$lib/i18n";
  import Brand from "$lib/Brand.svelte";

  let username = $state("");
  let password = $state("");
  let busy = $state(false);
  let err = $state<string | null>(null);

  async function submit(e: Event) {
    e.preventDefault();
    busy = true;
    err = null;
    try {
      await auth.login(username, password);
      // Same-origin relative paths only — mirrors /auth/link's open-redirect guard.
      // Lets the companion app's onboarding send someone through login and back
      // (/login?next=/onboard) instead of stranding them on the floorplan.
      const next = page.url.searchParams.get("next");
      goto(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch (e) {
      err = e instanceof Unauthorized
        ? t("login.invalid")
        : e instanceof Error
          ? e.message
          : t("login.failed");
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{t("login.title")}</title></svelte:head>

<div class="flex min-h-full items-center justify-center px-4">
  <form onsubmit={submit} class="w-full max-w-sm rounded-xl border border-dida-border bg-dida-panel p-6">
    <div class="mb-5 flex justify-center">
      <Brand size="md" />
    </div>

    <label class="block text-s text-dida-text-muted" for="u">{t("login.username")}</label>
    <input
      id="u" bind:value={username} autocomplete="username" required
      class="mb-3 mt-1 w-full"
    />

    <label class="block text-s text-dida-text-muted" for="p">{t("login.password")}</label>
    <input
      id="p" type="password" bind:value={password} autocomplete="current-password" required
      class="mb-4 mt-1 w-full"
    />

    {#if err}
      <Notice tone="err">{err}</Notice>
    {/if}

    <div class="grid"><Button tone="primary" type="submit" disabled={busy}
    >{busy ? t("login.submitting") : t("login.title")}</Button></div>
  </form>
</div>
