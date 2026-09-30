<script lang="ts">
  // Zigbee bridge tools, rendered inside the MQTT adapter's card — the same home
  // every other adapter's special UI has (HomeKit pairing, Tuya cloud pull,
  // ESPHome nodes), so Zigbee is not a page of its own.
  import { onDestroy, onMount } from "svelte";
  import { Button, Notice, Tag, dialog } from "$lib/kit";
  import { api, type ZigbeeMap, type ZigbeeNode } from "$lib/api";
  import { devices } from "$lib/store.svelte";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";

  let data = $state<ZigbeeMap | null>(null);
  let loadErr = $state<string | null>(null);
  let busy = $state(false);
  let avail = $state<boolean | null>(null);
  let availBusy = $state(false);

  // Pairing window: z2m opens it for a fixed span; we mirror that with a local
  // countdown so the button state matches reality even without a bridge push.
  let pairLeft = $state(0);
  let pairTimer: ReturnType<typeof setInterval> | null = null;
  let scanPoll: ReturnType<typeof setInterval> | null = null;

  // Inline rename (no window.prompt): one node edited at a time.
  let renaming = $state<string | null>(null);
  let renameTo = $state("");

  const scanning = $derived(data?.scanning ?? false);
  const nodes = $derived(data?.map?.nodes ?? []);
  const links = $derived(data?.map?.links ?? []);

  // The single number a person reads off a node: the best link it holds.
  function bestLqi(n: ZigbeeNode): number | null {
    const mine = links.filter((l) => l.source === n.ieee || l.target === n.ieee);
    const vals = mine.map((l) => l.lqi).filter((v): v is number => typeof v === "number");
    return vals.length ? Math.max(...vals) : null;
  }
  function linkCount(n: ZigbeeNode): number {
    return links.filter((l) => l.source === n.ieee || l.target === n.ieee).length;
  }
  const RANK: Record<string, number> = { coordinator: 0, router: 1, enddevice: 2 };
  const sorted = $derived(
    [...nodes].sort((a, b) => (RANK[a.type] ?? 3) - (RANK[b.type] ?? 3) || a.name.localeCompare(b.name)),
  );
  // The mesh table wears the SAME reachability verdict as every other surface —
  // the device row's, from the adapter — so a dead router can't sit here looking
  // like a healthy node with a merely quiet radio.
  function unreach(n: ZigbeeNode): boolean {
    if (!n.slug) return false;
    return devices.list.some((m) => m.deviceKey === n.slug && m.reachable === false);
  }

  function typeLabel(tp: string): string {
    if (tp === "coordinator") return t("zigbee.node.coordinator");
    if (tp === "router") return t("zigbee.node.router");
    if (tp === "enddevice") return t("zigbee.node.enddevice");
    return tp || "—";
  }
  const lastScan = $derived(
    data?.ts_ns
      ? dateTime(data.ts_ns / 1e6)
      : t("zigbee.map.never"),
  );

  async function load() {
    try {
      data = await api.zigbeeMap();
      loadErr = null;
      if (data.scanning && !scanPoll) startPoll();
    } catch (e) {
      loadErr = errMsg(e);
    }
    try {
      avail = (await api.zigbeeAvailability()).enabled;
    } catch {
      avail = null;
    }
  }

  async function toggleAvail() {
    availBusy = true;
    try {
      await api.zigbeeSetAvailability(!avail);
      avail = !avail;
    } catch (e) {
      loadErr = errMsg(e);
    } finally {
      availBusy = false;
    }
  }

  function startPoll() {
    scanPoll = setInterval(async () => {
      try {
        const next = await api.zigbeeMap();
        data = next;
        if (!next.scanning) stopPoll();
      } catch {
        stopPoll();
      }
    }, 3000);
  }
  function stopPoll() {
    if (scanPoll) clearInterval(scanPoll);
    scanPoll = null;
  }

  async function scan() {
    busy = true;
    try {
      data = await api.zigbeeScan();
      if (!scanPoll) startPoll();
    } catch (e) {
      loadErr = errMsg(e);
    } finally {
      busy = false;
    }
  }

  async function togglePair() {
    busy = true;
    try {
      if (pairLeft > 0) {
        await api.zigbeePermitJoin(false);
        stopPair();
      } else {
        await api.zigbeePermitJoin(true, 254);
        pairLeft = 254;
        pairTimer = setInterval(() => {
          pairLeft -= 1;
          if (pairLeft <= 0) stopPair();
        }, 1000);
      }
    } catch (e) {
      loadErr = errMsg(e);
    } finally {
      busy = false;
    }
  }
  function stopPair() {
    if (pairTimer) clearInterval(pairTimer);
    pairTimer = null;
    pairLeft = 0;
  }

  function startRename(n: ZigbeeNode) {
    renaming = n.ieee;
    renameTo = n.name;
  }
  async function commitRename(n: ZigbeeNode) {
    const to = renameTo.trim();
    if (!to || to === n.name) {
      renaming = null;
      return;
    }
    busy = true;
    try {
      await api.zigbeeRename(n.name, to);
      renaming = null;
      await load();
    } catch (e) {
      loadErr = errMsg(e);
    } finally {
      busy = false;
    }
  }

  async function remove(n: ZigbeeNode) {
    const ok = await dialog.confirm({
      title: t("zigbee.remove"),
      message: t("zigbee.remove.confirm", { name: n.name }),
      confirmLabel: t("zigbee.remove"),
    });
    if (!ok) return;
    busy = true;
    try {
      await api.zigbeeRemove(n.ieee, false);
      await load();
    } catch {
      // A device that can't be reached to leave cleanly needs a forced removal —
      // asked for explicitly, never the silent default.
      const force = await dialog.confirm({
        title: t("zigbee.remove"),
        message: t("zigbee.remove.force"),
        confirmLabel: t("zigbee.remove"),
      });
      if (force) {
        try {
          await api.zigbeeRemove(n.ieee, true);
          await load();
        } catch (e) {
          loadErr = errMsg(e);
        }
      }
    } finally {
      busy = false;
    }
  }

  onMount(load);
  onDestroy(() => {
    stopPair();
    stopPoll();
  });
</script>

<div class="mt-3 border-t border-dida-border pt-3">
  {#if loadErr}
    <Notice tone="warn">{loadErr}</Notice>
  {/if}

  <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
    <div class="flex items-center gap-2">
      <Button size="small" tone={pairLeft > 0 ? "danger" : "primary"} disabled={busy} onclick={togglePair}>
        {pairLeft > 0 ? t("zigbee.pair.close") : t("zigbee.pair.open")}
      </Button>
      {#if pairLeft > 0}
        <span class="text-m tabular-nums text-dida-accent">{t("zigbee.pair.window", { s: String(pairLeft) })}</span>
      {/if}
    </div>
    <div class="flex items-center gap-2">
      <Button size="small" disabled={availBusy} onclick={toggleAvail}>
        {avail ? t("zigbee.avail.disable") : t("zigbee.avail.enable")}
      </Button>
      <span class="flex items-center gap-1.5 text-s text-dida-text-muted" title={t("zigbee.avail.hint")}>
        <span class="h-1.5 w-1.5 rounded-full {avail ? 'bg-dida-ok' : 'bg-dida-text-faint'}"></span>
        {avail === null ? "—" : avail ? t("zigbee.avail.on") : t("zigbee.avail.off")}
      </span>
    </div>
    <div class="ml-auto flex items-center gap-2">
      <span class="text-s text-dida-text-faint">{t("zigbee.map.lastScan", { t: lastScan })}</span>
      <Button size="small" disabled={busy || scanning} onclick={scan}>
        {scanning ? t("zigbee.map.scanning") : t("zigbee.map.scan")}
      </Button>
    </div>
  </div>
  <p class="mt-1 text-s text-dida-text-faint">{t("zigbee.pair.hint")} {t("zigbee.map.hint")}</p>

  {#if data?.error}
    <Notice tone="warn">{data.error}</Notice>
  {/if}

  {#if nodes.length}
    <div class="mt-3 overflow-x-auto">
      <table class="w-full min-w-[36rem] text-m">
        <tbody class="divide-y divide-dida-border/60">
          {#each sorted as n (n.ieee)}
            <tr>
              <td class="py-2 pr-3">
                {#if renaming === n.ieee}
                  <input
                    class="w-full"
                    bind:value={renameTo}
                    onkeydown={(e) => e.key === "Enter" && commitRename(n)}
                  />
                {:else}
                  <span class="font-medium">{n.name}</span>
                {/if}
              </td>
              <td class="px-3 py-2 text-dida-text-muted">
                {typeLabel(n.type)}
                {#if unreach(n)}
                  <Tag tone="warn">{t("card.unreachable")}</Tag>
                {/if}
              </td>
              <td class="px-3 py-2 text-right tabular-nums">
                {#if bestLqi(n) !== null}
                  <span class="text-dida-text-faint">{t("zigbee.node.lqi")}</span> {bestLqi(n)}
                {:else}—{/if}
              </td>
              <td class="px-3 py-2 text-right tabular-nums text-dida-text-faint">
                {linkCount(n)} {t("zigbee.node.links")}
              </td>
              <td class="py-2 pl-3 text-right">
                {#if n.type !== "coordinator"}
                  <div class="flex justify-end gap-2">
                    {#if renaming === n.ieee}
                      <Button tone="primary" size="small" disabled={busy} onclick={() => commitRename(n)} label={t("zigbee.rename")}>✓</Button>
                    {:else}
                      <Button size="small" onclick={() => startRename(n)}>{t("zigbee.rename")}</Button>
                    {/if}
                    <Button tone="danger" size="small" disabled={busy} onclick={() => remove(n)}>{t("zigbee.remove")}</Button>
                  </div>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
