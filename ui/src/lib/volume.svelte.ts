// Global volume presets (Quiet/Normal/Loud …) + the green→red loudness tint.
// The presets are one-tap buttons on each AVR zone's volume row; the colour maps a
// level to a hue so a quiet zone reads green and a loud one red — on the buttons,
// the current-level marker and the active space tab.
import { api, type VolumePreset } from "$lib/api";

/** Green (quiet) → red (loud). Hue 130° at 0 down to 0° at 100. */
export function volumeColor(v: number, opts: { sat?: number; light?: number; alpha?: number } = {}): string {
  const { sat = 65, light = 45, alpha = 1 } = opts;
  const clamped = Math.max(0, Math.min(100, v));
  const hue = Math.round(130 * (1 - clamped / 100));
  return `hsl(${hue} ${sat}% ${light}% / ${alpha})`;
}

class VolumeStore {
  presets = $state<VolumePreset[]>([]);
  #loaded = false;

  /** Load the presets once (idempotent). Falls back to empty on error — the UI
   *  simply shows no preset buttons rather than breaking. */
  async load(): Promise<void> {
    if (this.#loaded) return;
    this.#loaded = true;
    try {
      this.presets = await api.getVolumePresets();
    } catch {
      this.#loaded = false; // let a later call retry
    }
  }

  /** Admin: persist a new preset list (server sorts ascending) and reflect it. */
  async save(presets: VolumePreset[]): Promise<void> {
    this.presets = await api.setVolumePresets(presets);
  }
}

export const volume = new VolumeStore();
