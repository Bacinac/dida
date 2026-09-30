<script lang="ts">
  // An article, and under it the way on to the assistant for whoever has one:
  // the same question the article answers, asked about what it did not.
  import { page } from "$app/state";
  import { Button, HelpPage, i18n } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { ui } from "$lib/shell.svelte";
  import { help } from "$lib/help";
</script>

<HelpPage {help} slug={page.params.slug ?? ""}>
  {#snippet after(article)}
    {#if auth.canSee("assistant") && auth.user?.assistant}
      <div>
        <Button onclick={() => ui.askAssistant(t("assistant.articleQuestion", { topic: article.title[i18n.locale] }))}>
          {t("assistant.askArticle")}
        </Button>
      </div>
    {/if}
  {/snippet}
</HelpPage>
