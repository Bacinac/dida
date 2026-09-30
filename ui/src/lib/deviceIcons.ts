// Floor-plan / device icon catalog — one consistent stroke set (24×24, drawn by
// DeviceIcon with a shared stroke style). Auto-typing uses the base names
// (light/switch/cover/lock/media/presence/sensor/button/other); the rest are
// pickable per device on the floor plan. Inner SVG markup only — DeviceIcon
// wraps it in the <svg> with stroke=currentColor.

export const ICONS: Record<string, string> = {
  // — base types (auto) —
  light: '<path d="M9 18h6"/><path d="M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.5c.8.8 1 1.3 1 2.5h6c0-1.2.2-1.7 1-2.5A6 6 0 0 0 12 3z"/>',
  strip: '<path d="M3 8h15a3 3 0 0 1 0 6H3z"/><path d="M6 11h.01M10 11h.01M14 11h.01M18 11h.01"/>',
  ceiling: '<path d="M3 4h18"/><path d="M8 4a4 4 0 0 0 8 0"/><path d="M7.5 10l-1 2.5M12 11v2.5M16.5 10l1 2.5"/>',
  pendant: '<path d="M12 3v3"/><path d="M7.5 12l4.5-6 4.5 6z"/><path d="M7.5 12h9"/><path d="M9 15l-1 2M12 16v2M15 15l1 2"/>',
  spot: '<rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 7l-3 6h12l-3-6"/><path d="M12 13v2"/>',
  switch: '<rect x="3" y="8" width="18" height="8" rx="4"/><circle cx="8" cy="12" r="2.4" fill="currentColor" stroke="none"/>',
  cover: '<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M4 8h16M4 13h16M4 18h16"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  media: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M10 9l4 3-4 3z" fill="currentColor" stroke="none"/><path d="M8 21h8"/>',
  remote: '<rect x="7" y="2" width="10" height="20" rx="3"/><circle cx="12" cy="6" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12.5" r="2.8"/><circle cx="12" cy="12.5" r="0.6" fill="currentColor" stroke="none"/><path d="M9.5 18.5h5"/>',
  presence: '<circle cx="12" cy="8" r="3.5"/><path d="M5 21c0-3.9 3.1-7 7-7s7 3.1 7 7"/>',
  sensor: '<path d="M3 12h4l2 6 4-14 2 8h6"/>',
  button: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/>',
  other: '<path d="M12 2l8 4.5v9L12 20l-8-4.5v-9z"/><path d="M12 20v-9M4 6.5l8 4.5 8-4.5"/>',

  // — pickable extras —
  plug: '<path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
  outlet: '<rect x="4" y="4" width="16" height="16" rx="3"/><circle cx="9.5" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="14.5" cy="12" r="1.1" fill="currentColor" stroke="none"/>',
  relay: '<rect x="3" y="7" width="18" height="10" rx="1"/><path d="M7 12h4l3-3"/><circle cx="7" cy="12" r="1.1" fill="currentColor" stroke="none"/>',
  valve: '<path d="M3 14h5M16 14h5"/><rect x="8" y="10" width="8" height="8" rx="1"/><path d="M12 10V6"/><path d="M9 6h6"/>',
  speaker: '<rect x="6" y="3" width="12" height="18" rx="4"/><circle cx="12" cy="14" r="3"/><path d="M12 7h.01"/>',
  player: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M10 8.5l4 2.5-4 2.5z" fill="currentColor" stroke="none"/><path d="M8 21h8"/>',
  avr: '<rect x="2" y="7" width="20" height="10" rx="1"/><circle cx="7" cy="12" r="1.6"/><circle cx="11" cy="12" r="1.6"/><rect x="15" y="10.5" width="5" height="3" rx="0.5"/>',
  gate: '<path d="M3 20V8h18v12"/><path d="M3 12h18M8 8v12M13 8v12M18 8v12"/>',
  door: '<path d="M6 21V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v17"/><path d="M4 21h16"/><circle cx="14" cy="12" r="1" fill="currentColor" stroke="none"/>',
  climate: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/><path d="M12 9v6"/>',
  fan: '<circle cx="12" cy="12" r="2"/><path d="M12 10c0-3-1-5-3-5s-2.5 3 0 4M14 12c3 0 5-1 5-3s-3-2.5-4 0M12 14c0 3 1 5 3 5s2.5-3 0-4M10 12c-3 0-5 1-5 3s3 2.5 4 0"/>',
  motion: '<circle cx="13" cy="4.5" r="1.5"/><path d="M12 22l1.5-6L10 14l1-4 3.5 2.5 2.5 1M10 12l-2 4"/>',
  camera: '<path d="M3 8h3l2-2h4l2 2h5a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.3"/>',
  siren: '<path d="M6 16v-5a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 21a2 2 0 0 0 4 0"/>',
  smoke: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/><circle cx="12" cy="12" r="0.8" fill="currentColor" stroke="none"/><path d="M12 5v1.6M12 17.4V19M5 12h1.6M17.4 12H19"/>',
  // Robot mower (the real device is one), not a push mower: dome chassis on two
  // wheels with a stub antenna.
  mower: '<path d="M3 15v-1c0-4.4 3.6-8 8-8h2c4.4 0 8 3.6 8 8v1"/><path d="M2 15h20"/><circle cx="7" cy="18" r="1.7"/><circle cx="17" cy="18" r="1.7"/><path d="M12 6V3.5"/>',
  water: '<path d="M12 3s6 7 6 11a6 6 0 0 1-12 0c0-4 6-11 6-11z"/>',
  energy: '<path d="M13 2L4 14h6l-1 8 9-12h-6z"/>',

  // — access / entrances —
  car: '<path d="M5 11l1.6-4A2 2 0 0 1 8.5 6h7a2 2 0 0 1 1.9 1.4L19 11"/><path d="M4 11h16v5a1 1 0 0 1-1 1h-1.5a1 1 0 0 1-1-1v-1H7.5v1a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/><circle cx="7.5" cy="13.5" r="1" fill="currentColor" stroke="none"/><circle cx="16.5" cy="13.5" r="1" fill="currentColor" stroke="none"/>',
  garage: '<path d="M3 21V9.5l9-4.5 9 4.5V21"/><path d="M6 21v-8h12v8"/><path d="M6 15.5h12M6 18h12"/>',
  barrier: '<path d="M5 22V5"/><circle cx="5" cy="4" r="1.4"/><path d="M5 8h15v3H5z"/><path d="M9 8v3M13 8v3M17 8v3"/>',
  doorbell: '<rect x="8" y="3" width="8" height="18" rx="2"/><circle cx="12" cy="8" r="1.7"/><circle cx="12" cy="8" r="0.4" fill="currentColor" stroke="none"/><path d="M10 13h4M10 16h4"/>',
  intercom: '<rect x="6" y="2" width="12" height="20" rx="2"/><rect x="9" y="5" width="6" height="4" rx="1"/><circle cx="10" cy="13" r="0.8" fill="currentColor" stroke="none"/><circle cx="14" cy="13" r="0.8" fill="currentColor" stroke="none"/><circle cx="10" cy="16.5" r="0.8" fill="currentColor" stroke="none"/><circle cx="14" cy="16.5" r="0.8" fill="currentColor" stroke="none"/>',
  key: '<circle cx="8" cy="9" r="4"/><path d="M11 12l8 8M17 18l2-2M15 16l2-2"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',

  // — climate / appliances —
  thermostat: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><path d="M12 5v3M12 12l3.5-2"/>',
  heater: '<rect x="4" y="6" width="16" height="12" rx="1"/><path d="M8 6v12M12 6v12M16 6v12"/><path d="M6 20v2M18 20v2"/>',
  // Free-standing patio (mushroom) heater: reflector hood, pole, base, radiant
  // elements — a distinct outdoor heater vs the indoor radiator above.
  patio_heater: '<path d="M6 9.5c0-2.6 2.7-4 6-4s6 1.4 6 4z"/><path d="M12 9.5V18"/><path d="M9.4 21l.6-3h4l.6 3z"/><path d="M9 11.5v2.2M12 11.5v2.6M15 11.5v2.2"/>',
  ac: '<rect x="3" y="6" width="18" height="7" rx="1.5"/><path d="M5 10h14"/><path d="M7 16c1.2-1 1.2-2.2 0-3.2M12 16c1.2-1 1.2-2.2 0-3.2M17 16c1.2-1 1.2-2.2 0-3.2"/>',
  // Robot vacuum, top view: shell with a front bumper seam + the lidar turret —
  // the classic "roomba" glyph. Side brushes tried and dropped: they smear into
  // noise at marker size and push the shape toward a steering wheel.
  vacuum: '<circle cx="12" cy="12" r="9"/><path d="M4.6 7.8h14.8"/><circle cx="12" cy="13.2" r="2.4"/><path d="M12 13.2v0.01"/>',
  washer: '<rect x="4" y="3" width="16" height="18" rx="2"/><circle cx="12" cy="13" r="5"/><circle cx="12" cy="13" r="2"/><circle cx="7" cy="6" r="0.6" fill="currentColor" stroke="none"/><circle cx="9.5" cy="6" r="0.6" fill="currentColor" stroke="none"/>',
  dryer: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 8h16"/><circle cx="7.5" cy="5.5" r="0.6" fill="currentColor" stroke="none"/><circle cx="16.5" cy="5.5" r="0.6" fill="currentColor" stroke="none"/><circle cx="12" cy="14.5" r="4.5"/><path d="M10 13.6c1.3-1.2 2.7 1.2 4 0"/>',
  dishwasher: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 8h16"/><circle cx="7.5" cy="5.5" r="0.6" fill="currentColor" stroke="none"/><circle cx="10" cy="5.5" r="0.6" fill="currentColor" stroke="none"/><path d="M8 11h8v7H8z"/><circle cx="12" cy="14.5" r="1.4"/>',
  microwave: '<rect x="3" y="6" width="18" height="12" rx="1.5"/><rect x="5" y="8" width="9" height="8" rx="1"/><path d="M17 9v6"/><circle cx="17" cy="9.5" r="0.5" fill="currentColor" stroke="none"/>',
  kettle: '<path d="M6 10h9l-1 8a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2z"/><path d="M15 11l3-2.5"/><path d="M8.5 10a3 3 0 0 1 5 0"/><path d="M9 6.5h3"/>',
  iron: '<path d="M3 16h12a4 4 0 0 0 4-4c0-1.2-1.2-2-3-2H8a5 5 0 0 0-5 4z"/><path d="M6 16v2M10 16v2M14 16v2"/>',
  fridge: '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M6 9h12"/><path d="M9 5v2M9 12v4"/>',
  oven: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 8h16"/><circle cx="7" cy="5.5" r="0.6" fill="currentColor" stroke="none"/><circle cx="10" cy="5.5" r="0.6" fill="currentColor" stroke="none"/><rect x="7" y="11" width="10" height="7" rx="1"/>',
  coffee: '<path d="M4 8h13v4a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/><path d="M17 9h2a2 2 0 0 1 0 4h-2"/><path d="M7 3v2M10 3v2M13 3v2"/>',
  tv: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  projector: '<rect x="2" y="7" width="12" height="9" rx="1.5"/><circle cx="10.5" cy="11.5" r="2.6"/><path d="M14.5 9l6.5-2.5M14.5 14l6.5 2.5"/><circle cx="5" cy="9.6" r="0.7" fill="currentColor" stroke="none"/>',
  laser: '<path d="M9.5 20.5h5l-1-2.5h-3z"/><path d="M12 18v-5.3"/><path d="M12 12.7 7 6.2M12 12.7V4.4M12 12.7 17 6.2"/><circle cx="7" cy="6.2" r="0.95" fill="currentColor" stroke="none"/><circle cx="12" cy="4.4" r="0.95" fill="currentColor" stroke="none"/><circle cx="17" cy="6.2" r="0.95" fill="currentColor" stroke="none"/>',
  music: '<path d="M9 17V5l10-2v12"/><circle cx="6.5" cy="17" r="2.4"/><circle cx="16.5" cy="15" r="2.4"/>',
  video: '<rect x="3" y="5" width="18" height="13" rx="2"/><path d="M10 9.2l5.5 3.3-5.5 3.3z" fill="currentColor" stroke="none"/>',
  flame: '<path d="M12 3c2 4 5 5 5 9a5 5 0 0 1-10 0c0-2 1-3.2 2-4.2.2 1.2 1 2.2 2 2.2 1.2-2 0-4.2 1-7z"/>',

  // — covers / openings —
  window: '<rect x="4" y="4" width="16" height="16" rx="1"/><path d="M12 4v16M4 12h16"/>',
  curtain: '<path d="M3 3h18"/><path d="M6 3c0 6-1 12 1 18M10 3c0 7 0 12-1 18M14 3c0 6 0 11 1 18M18 3c0 6 1 12-1 18"/>',

  // — utility / energy —
  solar: '<rect x="3" y="5" width="18" height="10" rx="1"/><path d="M3 8.5h18M3 11.5h18M9 5v10M15 5v10"/><path d="M12 15v5M9 20h6"/>',
  battery: '<rect x="3" y="8" width="16" height="8" rx="1.5"/><path d="M21 11v2"/><path d="M7 12h6"/>',
  ev: '<rect x="6" y="3" width="9" height="18" rx="2"/><path d="M11 6.5l-2 3.5h3l-2 3.5"/><path d="M15 8h2a2 2 0 0 1 2 2v4a1.4 1.4 0 0 1-2.8 0v-2"/>',
  pump: '<circle cx="11" cy="13" r="6"/><path d="M11 7V4h5"/><path d="M5 13H2M17 13h5"/>',
  router: '<rect x="4" y="14" width="16" height="6" rx="1"/><circle cx="8" cy="17" r="0.7" fill="currentColor" stroke="none"/><path d="M14 17h3"/><path d="M12 14v-2"/><path d="M9 9a5 5 0 0 1 6 0M7 6.5a8.5 8.5 0 0 1 10 0"/>',
  pool: '<path d="M2 15c1.8 0 1.8 1.4 3.6 1.4S7.4 15 9.2 15s1.8 1.4 3.6 1.4S14.6 15 16.4 15s1.8 1.4 3.6 1.4"/><path d="M2 19c1.8 0 1.8 1.4 3.6 1.4S7.4 19 9.2 19"/><path d="M7 13V6a2 2 0 0 1 4 0M15 13V6"/>',
};

// Order shown in the picker — grouped: access, lighting, climate, appliances,
// covers, media, sensors/security, garden/utility, misc.
export const ICON_LIST: string[] = [
  // access / entrances
  "door", "gate", "car", "garage", "barrier", "doorbell", "intercom", "lock", "key",
  // lighting
  "light", "ceiling", "pendant", "spot", "strip", "switch", "plug", "relay",
  // climate
  "climate", "thermostat", "heater", "patio_heater", "ac", "fan", "flame",
  // appliances
  "washer", "dryer", "dishwasher", "fridge", "oven", "microwave", "coffee", "kettle", "iron", "vacuum", "tv", "outlet",
  // covers / openings
  "cover", "curtain", "window",
  // media
  "media", "remote", "player", "speaker", "avr", "projector", "laser",
  // sensors / security
  "sensor", "motion", "presence", "camera", "smoke", "siren", "shield",
  // garden / utility / energy
  "valve", "pump", "mower", "pool", "water", "solar", "battery", "ev", "energy", "router",
  // misc
  "other",
];
