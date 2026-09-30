<script lang="ts">
  // Settings → Backup. Admin-only (guarded by the layout nav). Two independent
  // backups: the CONFIG database (pg_dump), and the HISTORY firehose (ClickHouse,
  // a .tar.gz of a FORMAT Native dump of all three history tables). Both restores
  // are destructive and confirmed.
  import { onMount } from "svelte";
  import { api, type BackupFile, type BackupJob, type BackupSchedule } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { Button, Card, Notice, SaveButton, Toggle, dialog, formatNumber, toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";

  let downloading = $state(false);
  let restoring = $state(false);
  let file = $state<File | null>(null);
  let err = $state<string | null>(null);
  let fileInput: HTMLInputElement | undefined = $state();

  // history (ClickHouse)
  let hist = $state<{ rows: number; bytes: number } | null>(null);
  let histDownloading = $state(false);
  let histRestoring = $state(false);
  let histFile = $state<File | null>(null);
  let histInput: HTMLInputElement | undefined = $state();

  // automatic (scheduled) backups
  const emptyJob = (keep: number): BackupJob => ({
    enabled: false, freq: "daily", time: "03:00", weekday: 0, keep, next_run: null,
  });
  let sched = $state<BackupSchedule>({ config: emptyJob(14), history: emptyJob(3) });
  let schedSnap = $state("");
  let saving = $state(false);
  let runningNow = $state(false);
  let files = $state<BackupFile[]>([]);

  const jobKey = (j: BackupJob) => `${j.enabled}|${j.freq}|${j.time}|${j.weekday}|${j.keep}`;
  const schedKey = (s: BackupSchedule) => jobKey(s.config) + "#" + jobKey(s.history);
  const dirty = $derived(schedSnap !== "" && schedKey(sched) !== schedSnap);

  // The two jobs, so the form is one row each (no duplicated markup).
  const JOBS = [
    { key: "config" as const, label: "backup.auto.config" as const },
    { key: "history" as const, label: "backup.auto.history" as const },
  ];

  // Literal keys so t() stays type-checked (a dynamic `backup.day.${d}` isn't).
  const DAY_KEYS = [
    "backup.day.0", "backup.day.1", "backup.day.2", "backup.day.3",
    "backup.day.4", "backup.day.5", "backup.day.6",
  ] as const;

  async function loadFiles() {
    try {
      files = await api.listBackups();
    } catch (e) {
      err = errMsg(e);
    }
  }

  onMount(async () => {
    try {
      hist = await api.historyInfo();
    } catch {
      /* history size is informational — the buttons still work without it */
    }
    try {
      sched = await api.getSchedule();
      schedSnap = schedKey(sched);
    } catch {
      /* leave defaults */
    }
    await loadFiles();
  });

  async function saveSchedule() {
    saving = true;
    err = null;
    try {
      const strip = (j: BackupJob) => ({ enabled: j.enabled, freq: j.freq, time: j.time, weekday: j.weekday, keep: j.keep });
      sched = await api.putSchedule({ config: strip(sched.config), history: strip(sched.history) });
      schedSnap = schedKey(sched);
      toasts.success(t("backup.auto.saved"));
    } catch (e) {
      err = errMsg(e);
    } finally {
      saving = false;
    }
  }

  async function runNow() {
    runningNow = true;
    err = null;
    try {
      const r = await api.runBackup();
      toasts.success(t("backup.auto.ranNow", { files: r.created.join(", ") || "—" }));
      await loadFiles();
    } catch (e) {
      err = errMsg(e);
    } finally {
      runningNow = false;
    }
  }

  async function downloadStored(name: string) {
    err = null;
    try {
      await saveBlob(await api.backupFile(name), name);
    } catch (e) {
      err = errMsg(e);
    }
  }

  async function deleteStored(name: string) {
    const ok = await dialog.confirm({
      title: t("backup.file.delTitle"),
      message: t("backup.file.delMsg", { name }),
      confirmLabel: t("common.delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteBackupFile(name);
      await loadFiles();
    } catch (e) {
      err = errMsg(e);
    }
  }

  function filenameFrom(r: Response, fallback: string): string {
    const m = (r.headers.get("Content-Disposition") || "").match(/filename="?([^"]+)"?/);
    return m ? m[1] : fallback;
  }

  async function saveBlob(r: Response, fallback: string) {
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filenameFrom(r, fallback);
    a.click();
    URL.revokeObjectURL(url);
  }

  async function download() {
    err = null;
    downloading = true;
    try {
      await saveBlob(await api.backup(), "dida-backup.dump");
    } catch (e) {
      err = errMsg(e);
    } finally {
      downloading = false;
    }
  }

  async function restore() {
    if (!file) return;
    const ok = await dialog.confirm({
      title: t("backup.restore.confirmTitle"),
      message: t("backup.restore.confirmMsg"),
      confirmLabel: t("backup.restore.confirmBtn"),
      danger: true,
    });
    if (!ok) return;
    err = null;
    restoring = true;
    try {
      const res = await api.restore(file);
      toasts.success(res.message);
      file = null;
      if (fileInput) fileInput.value = "";
    } catch (e) {
      err = errMsg(e);
    } finally {
      restoring = false;
    }
  }

  async function downloadHistory() {
    err = null;
    histDownloading = true;
    try {
      await saveBlob(await api.backupHistory(), "dida-history.tar.gz");
    } catch (e) {
      err = errMsg(e);
    } finally {
      histDownloading = false;
    }
  }

  async function restoreHistory() {
    if (!histFile) return;
    const ok = await dialog.confirm({
      title: t("backup.history.restoreConfirmTitle"),
      message: t("backup.history.restoreConfirmMsg"),
      confirmLabel: t("backup.restore.confirmBtn"),
      danger: true,
    });
    if (!ok) return;
    err = null;
    histRestoring = true;
    try {
      const res = await api.restoreHistory(histFile);
      toasts.success(res.message);
      histFile = null;
      if (histInput) histInput.value = "";
      hist = await api.historyInfo();
    } catch (e) {
      err = errMsg(e);
    } finally {
      histRestoring = false;
    }
  }

  const FILE_CLASS =
    "text-m text-dida-text-muted file:mr-3 file:rounded-md file:border-0 " +
    "file:bg-dida-panel-2 file:px-3 file:py-1.5 file:text-m file:text-dida-text hover:file:bg-dida-panel";

</script>

{#if err}
  <div class="mb-4"><Notice tone="err">{t("backup.err")}: {err}</Notice></div>
{/if}

<div class="grid max-w-2xl gap-4">
  <!-- App (Postgres config): download + restore in one card -->
  <Card>
    <h2 class="font-semibold">{t("backup.app.title")}</h2>
    <p class="mb-3 mt-1 text-m text-dida-text-muted">{t("backup.intro")}</p>
    <Button tone="primary" onclick={download} disabled={downloading}>
      {downloading ? t("backup.download.working") : t("backup.download.btn")}
    </Button>

    <div class="mt-4 border-t border-dida-border pt-4">
      <h3 class="text-m font-semibold">{t("backup.restoreSub")}</h3>
      <p class="mb-2 mt-1 text-m text-dida-text-muted">{t("backup.restore.desc")}</p>
      <div class="flex flex-wrap items-center gap-3">
        <input bind:this={fileInput} type="file" accept=".dump" onchange={(e) => (file = (e.target as HTMLInputElement).files?.[0] ?? null)} class={FILE_CLASS} />
        <Button tone="danger" onclick={restore} disabled={!file || restoring}>
          {restoring ? t("backup.restore.working") : t("backup.restore.btn")}
        </Button>
      </div>
    </div>
  </Card>

  <!-- History (ClickHouse): download + restore in one card -->
  <Card>
    <h2 class="font-semibold">{t("backup.history.title")}</h2>
    <p class="mt-1 text-m text-dida-text-muted">{t("backup.history.desc")}</p>
    <p class="mb-3 mt-1 text-s text-dida-text-faint">
      {#if hist}
        {t("backup.history.size", { rows: formatNumber(hist.rows, { maximumFractionDigits: 1 }), mb: formatNumber(Math.round(hist.bytes / 1048576), { maximumFractionDigits: 1 }) })}
      {:else}
        &nbsp;
      {/if}
    </p>
    <Button tone="primary" onclick={downloadHistory} disabled={histDownloading}>
      {histDownloading ? t("backup.download.working") : t("backup.history.download")}
    </Button>

    <div class="mt-4 border-t border-dida-border pt-4">
      <h3 class="text-m font-semibold">{t("backup.restoreSub")}</h3>
      <p class="mb-2 mt-1 text-m text-dida-text-muted">{t("backup.history.restoreDesc")}</p>
      <div class="flex flex-wrap items-center gap-3">
        <input bind:this={histInput} type="file" accept=".tar.gz,.gz" onchange={(e) => (histFile = (e.target as HTMLInputElement).files?.[0] ?? null)} class={FILE_CLASS} />
        <Button tone="danger" onclick={restoreHistory} disabled={!histFile || histRestoring}>
          {histRestoring ? t("backup.restore.working") : t("backup.restore.btn")}
        </Button>
      </div>
    </div>
  </Card>

  <!-- Automatic (scheduled) backups -->
  <Card>
    <h2 class="font-semibold">{t("backup.auto.title")}</h2>
    <p class="mb-3 mt-1 text-m text-dida-text-muted">{t("backup.auto.desc", { path: "/backups" })}</p>

    <div class="flex flex-col gap-3 text-m">
      {#each JOBS as jb (jb.key)}
        {@const job = sched[jb.key]}
        <div class="flex flex-wrap items-center gap-2">
          <label class="flex w-28 shrink-0 items-center gap-2 font-medium">
            <Toggle size="small" checked={job.enabled} onclick={() => (job.enabled = !job.enabled)} label={t(jb.label)} />
            {t(jb.label)}
          </label>
          {#if job.enabled}
            <select bind:value={job.freq} class="w-24" aria-label={t("backup.auto.freq")}>
              <option value="daily">{t("backup.auto.daily")}</option>
              <option value="weekly">{t("backup.auto.weekly")}</option>
            </select>
            {#if job.freq === "weekly"}
              <select bind:value={job.weekday} class="w-28" aria-label={t("backup.auto.weekday")}>
                {#each DAY_KEYS as key, d (d)}<option value={d}>{t(key)}</option>{/each}
              </select>
            {/if}
            <input type="time" bind:value={job.time} class="w-24" aria-label={t("backup.auto.time")} />
            <span class="flex items-center gap-1 text-s text-dida-text-muted">
              {t("backup.auto.keep")}
              <input type="number" min="1" max="365" bind:value={job.keep} class="w-14" aria-label={t("backup.auto.keep")} />
            </span>
            {#if job.next_run}
              <span class="text-s text-dida-text-faint">· {dateTime(job.next_run)}</span>
            {/if}
          {/if}
        </div>
      {/each}

      <div class="mt-1 flex items-center gap-3">
        <SaveButton size="small" {dirty} {saving} onclick={saveSchedule} />
        <Button onclick={runNow} disabled={runningNow}>
          {runningNow ? t("backup.auto.running") : t("backup.auto.runNow")}
        </Button>
      </div>
    </div>

    <div class="mt-4 border-t border-dida-border pt-4">
      <h3 class="mb-2 text-m font-semibold">{t("backup.auto.files")}</h3>
      {#if files.length === 0}
        <p class="text-m text-dida-text-faint">{t("backup.auto.noFiles")}</p>
      {:else}
        <div class="grid gap-1.5">
          {#each files as f (f.name)}
            <div class="flex flex-wrap items-center justify-between gap-2 rounded-md bg-dida-panel-2 px-3 py-2 text-m">
              <div class="min-w-0">
                <div class="truncate font-mono text-s">{f.name}</div>
                <div class="text-s text-dida-text-faint">
                  {formatNumber(Math.round(f.bytes / 1024), { maximumFractionDigits: 1 })} kB · {dateTime(f.mtime)}
                </div>
              </div>
              <div class="flex items-center gap-2">
                <Button size="small" onclick={() => downloadStored(f.name)}>{t("common.download")}</Button>
                <Button tone="danger" size="small" onclick={() => deleteStored(f.name)}>{t("common.delete")}</Button>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </Card>
</div>
