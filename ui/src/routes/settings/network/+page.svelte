<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Card, Field, Notice, SaveButton } from "$lib/kit";
  import { api, type LanStatus } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { Section } from "$lib/ui";
  import { auth } from "$lib/auth.svelte";
  import { devices } from "$lib/store.svelte";
  import EntityPicker, { pickerItem } from "$lib/EntityPicker.svelte";

  let loadErr = $state<string | null>(null);

  // --- The host's own interfaces. lanprobe (host-net) reports every leg the host
  //     addressed — iface/address/MAC — with no router integration. Read-only:
  //     DIDA does NOT own them (the host does; on an LXC the VLAN tag is set on
  //     Proxmox), so a fixed address is a DHCP reservation on the router. Only the
  //     discovery scope is editable. ---
  let lanStatus = $state<LanStatus>({});

  // --- This installation's own addresses. They live in the DB (an .env value is
  //     only what install.sh detected at setup), so moving the host is edited here
  //     rather than on the box. ---
  let host = $state({ lan_ip: "", opus_url: "", app_url: "", net_parent: "" });
  let hostSaved = $state({ lan_ip: "", opus_url: "", app_url: "", net_parent: "" });
  // The OPUS token is a secret: never read back, only written when typed.
  let opusToken = $state("");
  let opusConfigured = $state(false);
  let opusCheck = $state<{ ok: boolean; detail: string } | null>(null);
  let opusChecking = $state(false);
  let radioPlayer = $state("");
  let radioPlayerSaved = $state("");
  const playerItems = $derived(devices.list.filter((d) => "media_transport" in d.caps).map(pickerItem));
  let hostBusy = $state(false);
  let hostMsg = $state("");
  const hostDirty = $derived(
    JSON.stringify(host) !== JSON.stringify(hostSaved) || !!opusToken.trim() || radioPlayer !== radioPlayerSaved,
  );
  async function saveHost() {
    hostBusy = true;
    hostMsg = "";
    try {
      await api.updateSettings({
        ...host,
        ...(opusToken.trim() ? { opus_token: opusToken.trim() } : {}),
        // sent only when changed: a stored player that has since gone must not block saving an address
        ...(radioPlayer !== radioPlayerSaved ? { radio_player: radioPlayer } : {}),
      });
      hostSaved = { ...host };
      radioPlayerSaved = radioPlayer;
      opusToken = "";
      hostMsg = t("settings.saved");
      await load();
    } catch (e) {
      hostMsg = errMsg(e);
    } finally {
      hostBusy = false;
    }
  }
  // One real call to the player with what is typed (or what is stored), so a
  // wrong address or token is found here and not on the media page.
  async function checkOpus() {
    opusChecking = true;
    opusCheck = null;
    try {
      opusCheck = await api.testSettings("opus", opusToken.trim() || undefined, host.opus_url.trim() || undefined);
    } catch (e) {
      opusCheck = { ok: false, detail: errMsg(e) };
    } finally {
      opusChecking = false;
    }
  }
  let subnets = $state("");
  let subnetsSaved = $state("");
  let subnetsBusy = $state(false);
  let subnetsMsg = $state("");
  async function saveSubnets() {
    subnetsBusy = true;
    subnetsMsg = "";
    try {
      await api.updateSettings({ discovery_subnets: subnets });
      subnetsSaved = subnets;
      subnetsMsg = t("settings.saved");
    } catch (e) {
      subnetsMsg = errMsg(e);
    } finally {
      subnetsBusy = false;
    }
  }

  // --- VLAN: DIDA's OWN presence on isolated IoT VLAN(s). netmgr brings up a
  //     macvlan sub-interface on ens18.<id> and DHCPs on it — DIDA owns that
  //     interface end-to-end, so here MAC/hostname ARE editable. ---
  let vlans = $state("");
  let vlansSaved = $state("");
  let vlanBusy = $state(false);
  let vlanMsg = $state("");
  let vlanStatus = $state<Record<string, { iface: string; address: string; mac?: string }>>({});
  // Per-VLAN MAC/hostname overrides (a fixed MAC → the router can reserve a stable
  // lease; hostname identifies DIDA in the DHCP lease table).
  let vlanConfig = $state<Record<string, { mac?: string; hostname?: string }>>({});
  let vlanConfigSaved = $state<Record<string, { mac?: string; hostname?: string }>>({}); // snapshot → dirty check
  let vlanCfgBusy = $state(false);
  let vlanCfgMsg = $state("");
  // Unsaved MAC/hostname edits for THIS VLAN (per-vid so one card's Save doesn't
  // light up when another VLAN is edited).
  const vlanCfgDirty = (vid: string) =>
    JSON.stringify(vlanConfig[vid] ?? {}) !== JSON.stringify(vlanConfigSaved[vid] ?? {});

  // A manually-typed subnet that a VLAN foot already covers is redundant — the foot
  // supersedes a routed scan of the same range. Block Save early with a clear reason
  // (the backend rejects it too; this is the immediate, localised feedback).
  function ipToInt(ip: string): number | null {
    const parts = ip.split(".");
    if (parts.length !== 4) return null;
    let n = 0;
    for (const p of parts) {
      const o = Number(p);
      if (!Number.isInteger(o) || o < 0 || o > 255) return null;
      n = n * 256 + o;
    }
    return n >>> 0;
  }
  // Is subnet `a` (cidr) contained in / equal to subnet `b` (cidr)?
  function subnetOf(a: string, b: string): boolean {
    const [aIp, aPfx] = a.split("/");
    const [bIp, bPfx] = b.split("/");
    const ai = ipToInt(aIp), bi = ipToInt(bIp);
    if (ai === null || bi === null) return false;
    const ap = aPfx === undefined ? 32 : Number(aPfx);
    const bp = bPfx === undefined ? 32 : Number(bPfx);
    if (!Number.isInteger(ap) || !Number.isInteger(bp) || ap < bp) return false;
    const mask = bp === 0 ? 0 : (0xffffffff << (32 - bp)) >>> 0;
    return (ai & mask) === (bi & mask);
  }
  // First typed subnet that collides with a VLAN foot (→ {tok, vid}), else null.
  const subnetConflict = $derived.by(() => {
    const vnets = Object.entries(vlanStatus)
      .map(([vid, s]) => ({ vid, cidr: s.address }))
      .filter((v) => v.cidr?.includes("/"));
    for (const tok of subnets.split(",").map((s) => s.trim()).filter(Boolean)) {
      for (const v of vnets) if (subnetOf(tok, v.cidr)) return { tok, vid: v.vid };
    }
    return null;
  });

  async function load() {
    try {
      const st = await api.getSettings();
      lanStatus = st.lan_status ?? {};
      subnets = st.discovery_subnets ?? "";
      subnetsSaved = subnets;
      vlans = st.managed_vlans ?? "";
      vlansSaved = vlans;
      vlanStatus = st.vlan_status ?? {};
      vlanConfig = st.vlan_config ?? {};
      vlanConfigSaved = $state.snapshot(vlanConfig);
      host = {
        lan_ip: st.lan_ip ?? "",
        opus_url: st.opus_url ?? "",
        app_url: st.app_url ?? "",
        net_parent: st.net_parent ?? "",
      };
      hostSaved = { ...host };
      radioPlayer = st.radio_player ?? "";
      radioPlayerSaved = radioPlayer;
      opusConfigured = !!st.opus_configured;
      loadErr = null;
    } catch (e) {
      loadErr = errMsg(e);
    }
  }
  onMount(load);

  async function saveVlans() {
    vlanBusy = true;
    vlanMsg = "";
    try {
      await api.updateSettings({ managed_vlans: vlans });
      vlansSaved = vlans;
      vlanMsg = t("settings.saved");
      setTimeout(load, 4000); // netmgr reconciles on its poll; catch the new status
    } catch (e) {
      vlanMsg = errMsg(e);
    } finally {
      vlanBusy = false;
    }
  }
  function vlanCfg(vid: string, field: "mac" | "hostname", value: string) {
    vlanConfig[vid] = { ...(vlanConfig[vid] ?? {}), [field]: value };
  }
  async function saveVlanConfig() {
    vlanCfgBusy = true;
    vlanCfgMsg = "";
    try {
      await api.updateSettings({ vlan_config: vlanConfig });
      vlanConfigSaved = $state.snapshot(vlanConfig);
      // No restart: netmgr reconnects with the new MAC on its ~10s poll and DHCP
      // hands out the new lease. Keep refreshing until it lands (no manual reload).
      vlanCfgMsg = t("vlan.applying");
      for (const ms of [4000, 8000, 12000, 18000, 25000]) setTimeout(load, ms);
    } catch (e) {
      vlanCfgMsg = errMsg(e);
    } finally {
      vlanCfgBusy = false;
    }
  }
</script>

<svelte:head><title>{t("nav.network")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  {#if loadErr}
    <Notice tone="err">{loadErr}</Notice>
  {/if}

  <!-- A single "network presence" tile — used identically for the LAN and each
       VLAN so both segments read the same way. `input` swaps the value for a field. -->
  {#snippet tile(label: string, value: string | undefined, accent: boolean)}
    <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
      <p class="text-s text-dida-text-muted">{label}</p>
      <!-- A field's box with a transparent border, so a read-only value lines up
           with an editable input in the same row (see the VLAN card). -->
      <p class="mt-1 break-all border border-transparent p-(--field-pad) font-mono text-(length:--field-fs) {accent && value ? 'text-dida-ok' : ''}">{value || "—"}</p>
    </div>
  {/snippet}

  <!-- The host's interfaces (read-only, self-discovered) + editable discovery scope -->
  <Section title={t("net.lanTitle")}>
    <Card>
      {#if !lanStatus.interfaces?.some((i) => i.lan)}
        <Notice tone="warn">{t("net.lanMissing", { address: lanStatus.address || "—" })}</Notice>
      {/if}
      <!-- Same tile row as a VLAN card: the leg's role is the first tile's label. -->
      <div class="flex flex-col gap-3">
        {#each lanStatus.interfaces ?? [] as i (i.iface)}
          <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {@render tile(i.lan ? t("net.roleLan") : i.default_route ? t("net.roleDefault") : t("net.iface"), i.iface, false)}
            {@render tile(t("net.address"), i.address, i.lan)}
            {@render tile(t("net.mac"), i.mac, false)}
            {@render tile(t("net.hostname"), lanStatus.hostname, false)}
          </div>
        {/each}
      </div>
    </Card>
    <Card>
      <Field label={t("discover.subnetsLabel")} hint={t("discover.subnetsHelp")} id="iot-subnets">
        <div class="flex flex-wrap items-center gap-2">
          <input id="iot-subnets" bind:value={subnets} placeholder="203.0.113.0/24" autocomplete="off" class="w-full min-w-64 flex-1" />
          <SaveButton size="small" dirty={subnets.trim() !== subnetsSaved.trim()} saving={subnetsBusy} blocked={!!subnetConflict} onclick={saveSubnets} />
          {#if subnetConflict}
            <Notice tone="warn">{t("net.subnetVlanConflict", { subnet: subnetConflict.tok, vid: subnetConflict.vid })}</Notice>
          {:else if subnetsMsg}
            <Notice>{subnetsMsg}</Notice>
          {/if}
        </div>
      </Field>
    </Card>
  </Section>

  <!-- This installation's own addresses — what DIDA calls itself on the LAN and to
       the outside world. Stored in the DB; blank falls back to the env seed. -->
  <Section title={t("net.hostTitle")}>
    <Card>
      <Field label={t("net.lanIpLabel")} hint={t("net.lanIpHelp")} id="lan-ip">
        <input id="lan-ip" bind:value={host.lan_ip} placeholder="192.0.2.10"
          autocomplete="off" class="w-full font-mono" />
      </Field>
      <Field label={t("net.opusUrlLabel")} hint={t("net.opusUrlHelp")} id="opus-url">
        <input id="opus-url" bind:value={host.opus_url} placeholder="http://192.0.2.102:8098"
          autocomplete="off" class="w-full" />
      </Field>
      <Field label={t("net.opusTokenLabel")} hint={t("net.opusTokenHelp")} id="opus-token">
        <div class="flex flex-wrap items-center gap-2">
          <input id="opus-token" type="password" bind:value={opusToken} autocomplete="new-password"
            placeholder={opusConfigured ? t("settings.keySavedPlaceholder") : ""}
            class="w-full font-mono min-w-60 flex-1" />
          <span class="text-s {opusConfigured ? 'text-dida-ok' : 'text-dida-text-faint'}">{opusConfigured ? t("net.opusConfigured") : t("net.opusMissing")}</span>
          <Button size="small" onclick={checkOpus} disabled={opusChecking}>{t("net.opusCheck")}</Button>
          {#if opusCheck}<span class="text-s {opusCheck.ok ? 'text-dida-ok' : 'text-dida-danger'}">{opusCheck.detail}</span>{/if}
        </div>
      </Field>
      <Field label={t("net.radioPlayerLabel")} hint={t("net.radioPlayerHelp")}>
        <EntityPicker bind:value={radioPlayer} items={playerItems} placeholder={t("net.radioPlayerPick")} />
      </Field>
      <Field label={t("net.appUrlLabel")} hint={t("net.appUrlHelp")} id="app-url">
        <input id="app-url" bind:value={host.app_url} placeholder="https://dida.example.com"
          autocomplete="off" class="w-full" />
      </Field>
      <Field label={t("net.parentLabel")} hint={t("net.parentHelp")} id="net-parent">
        <div class="flex flex-wrap items-center gap-2">
          <input id="net-parent" bind:value={host.net_parent} placeholder="eth0"
            autocomplete="off" class="w-full min-w-40" />
          <SaveButton size="small" dirty={hostDirty} saving={hostBusy} onclick={saveHost} />
          {#if hostMsg}<Notice>{hostMsg}</Notice>{/if}
        </div>
      </Field>
    </Card>
  </Section>

  <!-- VLAN: DIDA's own presence on isolated IoT VLAN(s) via netmgr. Same tile grid
       as LAN; here MAC/Name are editable (DIDA owns the macvlan iface it creates). -->
  <Section title={t("net.vlanTitle")}>
    <Card>
      <Field label={t("vlan.label")} hint={t("vlan.help")} id="managed-vlans">
        <div class="flex flex-wrap items-center gap-2">
          <input id="managed-vlans" bind:value={vlans} placeholder="20" autocomplete="off" class="w-full min-w-64 flex-1" />
          <SaveButton size="small" dirty={vlans.trim() !== vlansSaved.trim()} saving={vlanBusy} onclick={saveVlans} />
          {#if vlanMsg}<Notice>{vlanMsg}</Notice>{/if}
        </div>
      </Field>
    </Card>

    {#each Object.entries(vlanStatus) as [vid, s] (vid)}
      <!-- The VLAN id IS this card's identity → it's the first tile's label (its
           value is the sub-interface), so the card has no separate header. Same tile
           grid as LAN; MAC/Name editable (DIDA owns the macvlan iface it creates). -->
      <Card>
        <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {@render tile(`VLAN ${vid}`, s.iface, false)}
          {@render tile(t("net.address"), s.address || t("vlan.pending"), !!s.address)}
          <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
            <p class="text-s text-dida-text-muted">{t("net.mac")}</p>
            <input value={vlanConfig[vid]?.mac ?? ""} oninput={(e) => vlanCfg(vid, "mac", e.currentTarget.value)}
              placeholder={s.mac || "aa:bb:cc:dd:ee:ff"} autocomplete="off" spellcheck="false" class="w-full font-mono mt-1" />
          </div>
          <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
            <p class="text-s text-dida-text-muted">{t("net.hostname")}</p>
            <input value={vlanConfig[vid]?.hostname ?? ""} oninput={(e) => vlanCfg(vid, "hostname", e.currentTarget.value)}
              placeholder="dida-iot" autocomplete="off" spellcheck="false" class="w-full mt-1" />
          </div>
        </div>
        <div class="mt-3 flex items-center justify-between gap-3">
          <p class="text-s text-dida-text-faint">{t("vlan.reserveHint")}</p>
          <SaveButton size="small" dirty={vlanCfgDirty(vid)} saving={vlanCfgBusy} onclick={saveVlanConfig} />
        </div>
        {#if vlanCfgMsg}<div class="mt-2"><Notice>{vlanCfgMsg}</Notice></div>{/if}
      </Card>
    {/each}
  </Section>
{/if}
