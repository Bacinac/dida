<script lang="ts">
  // The Energy page — a whole-house energy dashboard. Per-device history lives on
  // the device itself (floor-plan click), so there is no per-device explorer here.
  import { t } from "$lib/i18n";
  import EnergyDashboard from "$lib/EnergyDashboard.svelte";
</script>

<svelte:head><title>{t("nav.history")}</title></svelte:head>

<EnergyDashboard />
