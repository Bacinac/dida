<script lang="ts">
  // Android TV (Shield) presentation: the live screen at its real 16:9 aspect + the
  // basic remote ALWAYS visible below (not an overlay). The remote drives the box
  // directly over ADB — its own nav press-buttons + this device as the transport /
  // volume target — so it's reliable regardless of the Harmony activity.
  import { t } from "$lib/i18n";
  import { Tag } from "$lib/kit";
  import { mediaArt, mediaTitle } from "$lib/media";
  import Remote from "$lib/Remote.svelte";
  import { devices, type Device } from "$lib/store.svelte";

  // `channelLabel` = the live-TV channel DIDA last tuned (DRM apps like Xplore black
  // out the screencap, so the channel name is the only feedback we can show).
  let { device, channelLabel = null }: { device: Device; channelLabel?: string | null } = $props();

  const art = $derived(mediaArt(device));
  const title = $derived(mediaTitle(device) ?? device.name);
  const members = $derived(device.deviceKey ? devices.membersOf(device.deviceKey) : [device]);

  let artFailed = $state(false);
  $effect(() => {
    void art; // reset the fallback when a fresh snapshot URL arrives
    artFailed = false;
  });
</script>

<div class="space-y-3">
  <!-- Live screen — the device's real 16:9 aspect (a screencap of what's on). -->
  <div class="relative aspect-video w-full overflow-hidden rounded-xl border border-dida-border bg-black">
    {#if art && !artFailed}
      <img src={art} alt="" onerror={() => (artFailed = true)} class="h-full w-full object-contain" />
    {:else}
      <div class="grid h-full w-full place-items-center text-dida-text-faint">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" class="size-12">
          <rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" />
        </svg>
      </div>
    {/if}
    <div class="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 bg-gradient-to-t from-black/75 to-transparent p-2.5">
      <span class="truncate text-m font-semibold text-white">{title}{#if channelLabel} · {channelLabel}{/if}</span>
      <Tag onpicture tone="err"><span class="inline-block size-1.5 animate-pulse rounded-full bg-current"></span>{t("media.live")}</Tag>
    </div>
  </div>

  <!-- Basic commands, always visible. -->
  <div class="rounded-xl border border-dida-border bg-dida-panel p-4">
    <Remote {members} player={device} volumeDevice={device} showSource={false} layout="split" />
  </div>
</div>
