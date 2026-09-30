<script lang="ts">
  // The stations OPUS keeps, in its order, put on the player the host passes in.
  // Nothing is edited here — a station is added, renamed or removed in OPUS.
  import { onMount } from "svelte";
  import { api, type OpusStation } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { gradient } from "$lib/media";
  import type { Device } from "$lib/store.svelte";

  let { player, onplayed }: { player: Device | null; onplayed?: () => void } = $props();

  let stations = $state<OpusStation[]>([]);
  let err = $state<string | null>(null);

  onMount(async () => {
    try { stations = await api.opusStations(); } catch (e) { err = errMsg(e); }
  });
  function play(s: OpusStation): void {
    if (!player) return;
    err = null;
    api.opusPlay({ player_id: player.entityId, kind: "station", id: s.id })
      .then(() => onplayed?.())
      .catch((e) => (err = errMsg(e)));
  }
</script>

<div class="space-y-4">

  {#if !player}<p class="text-s text-dida-text-faint">{t("media.noPlayers")}</p>{/if}

  {#if stations.length}
    <div role="list" class="grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-6">
      {#each stations as s (s.id)}
        <div role="listitem" class="group relative overflow-hidden rounded-lg">
          <button type="button" onclick={() => play(s)} disabled={!player} title={s.genre} class="block w-full text-left disabled:opacity-40">
            <!-- A solid white tile with padding: logos carry wildly different baked-in
                 backgrounds, and one ground makes the grid read as a single wall. -->
            <div class="aspect-square w-full bg-white p-3">
              {#if s.logo}<img src={api.opusArt(s.logo, 256)} alt="" loading="lazy" class="h-full w-full object-contain" />
              {:else}<div class="h-full w-full" style="background:{gradient(s.name)}"></div>{/if}
            </div>
            <div class="absolute inset-0 flex flex-col justify-end bg-gradient-to-t from-black/70 to-transparent p-1.5">
              <p class="truncate text-xs font-semibold text-white">{s.name}</p>
            </div>
          </button>
        </div>
      {/each}
    </div>
  {:else if !err}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("media.noMusic")}</p>
  {/if}

  {#if err}<p class="text-s text-dida-danger">{err}</p>{/if}
</div>
