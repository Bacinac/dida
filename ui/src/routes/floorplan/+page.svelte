<script lang="ts">
  import { onMount, untrack } from "svelte";
  import { SUBSECTION_TITLE_CLASS } from "$lib/ui";
  import { devices, type Device } from "$lib/store.svelte";
  import { errMsg } from "$lib/errors";
  import { api, type VacuumLive, type VacuumMap, type VacuumMapEntry, type VacuumPlacement, type FloorPoly, type Floor, type Area, type Border, type SensorConfig } from "$lib/api";
  import { capLabel, capMeta, formatValue, stateColor, optionLabel } from "$lib/capabilities";
  import { primaryCap, readingDefaultIn, type RoomCap, type RoomReading } from "$lib/roomSensors";
  import { ICON_LIST } from "$lib/deviceIcons";
  import { pointInPolygon, polygonCentroid, toPoints } from "$lib/floorGeometry";
  import { buildGlows, buildPlanItems, buildRoomLabels, DEFAULT_R, isPlacedAny, isPlug, isSensorLabel, itemPosOn, opensControls, planCandidates, type PlaceItem, type RoomLabelItem } from "$lib/floorplanItems";
  import FloorControl from "$lib/FloorControl.svelte";
  import DeviceDetail from "$lib/DeviceDetail.svelte";
  import FloorGlow from "$lib/FloorGlow.svelte";
  import FloorplanPlaceholder from "$lib/FloorplanPlaceholder.svelte";
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import FloorMarker from "$lib/FloorMarker.svelte";
  import FloorSensorLabel from "$lib/FloorSensorLabel.svelte";
  import RoomSensorLabel from "$lib/RoomSensorLabel.svelte";
  import FloorHistory from "$lib/FloorHistory.svelte";
  import FloorReplay from "$lib/FloorReplay.svelte";
  import { replay } from "$lib/replay.svelte";
  import PresencePanel from "$lib/PresencePanel.svelte";
  import { Button, PageActions, Picks, SaveButton, formatNumber, toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { ui } from "$lib/shell.svelte";

  let floors = $state<Floor[]>([]);
  let floorsLoaded = $state(false); // gates the sample-plan placeholder (no flash before the real scaffold)
  const sortedFloors = $derived([...floors].sort((a, b) => a.sort_order - b.sort_order));
  const GRID = 2.5;          // snap step for device markers (% of plan)

  let floor = $state("");
  let edit = $state(false);
  let mode = $state<"devices" | "plan">("devices"); // edit sub-mode

  // ── border editor (the plan's single source of truth) ──
  // Everything is a DRAFT with an undo stack; nothing persists until Save. Rooms are
  // re-derived live from the draft, so every border edit shows its effect at once.
  const BORDER_T = 0.8;                           // drawn-border thickness (% of plan) — matches
                                                  // the detector's uniform BORDER_UNIFORM
  const SNAP_R = 1.5;                             // border-to-border snap radius (% of plan)
  let borderDraft = $state<Border[]>([]);         // current editable borders
  let borderSaved = $state<string>("[]");         // JSON of the last-saved set (dirty tracking)
  let undoStack = $state<Border[][]>([]);         // pre-change snapshots, newest last
  let selBorder = $state<number | null>(null);    // selected border index
  let addingBorder = $state(false);               // "add border" armed → drag to draw
  let drawBorder = $state<Border | null>(null);   // the border being drawn (preview)
  let snapDot = $state<{ x: number; y: number } | null>(null); // active snap indicator
  let borderBusy = $state(false);
  const bordersDirty = $derived(JSON.stringify(borderDraft) !== borderSaved);
  const snapHalf = (v: number): number => clamp(Math.round(v * 2) / 2); // 0.5 % grid fallback

  // ── rooms: live-derived from the border draft ──
  let rooms = $state<FloorPoly[]>([]);
  let deriving = $state(false);

  // ── room popover + merge flow ──
  interface RoomItem { poly: FloorPoly; areaId: number | null; cx: number; cy: number; n: number | null }
  let roomSel = $state<RoomItem | null>(null);    // popover target
  let mergeFrom = $state<RoomItem | null>(null);  // armed: waiting for the neighbour click
  let mergePreview = $state<{ borders: Border[]; removed: Border[]; rooms: FloorPoly[]; from: RoomItem; toAreaId: number | null } | null>(null);
  let mergeMsg = $state<string | null>(null);     // transient "not adjacent" notice
  let mergeMsgTimer: ReturnType<typeof setTimeout> | null = null;

  let picking = $state<string | null>(null);
  let selected = $state<PlaceItem | null>(null);
  let configItem = $state<PlaceItem | null>(null);   // marker whose icon/size/glow panel is open
  // Expanded room label (tap a room's temperature) + a value's history popover.
  let roomOpen = $state<RoomLabel | null>(null);
  let hist = $state<{ readings: { entityId: string; cap: string }[]; cap: string; name: string; x: number; y: number } | null>(null);
  let detailId = $state<string | null>(null);   // device-detail panel target
  let histArm = false; // backdrop close armed by a pointerdown ON the backdrop (not a stray hold-release click)
  // Tap on a vision zone's dot → what the camera sees right now (live snapshot popover).
  let camSnap = $state<{ eid: string; name: string; x: number; y: number } | null>(null);
  let camTick = $state(0);
  $effect(() => {
    if (!camSnap) return;
    const t = setInterval(() => (camTick += 1), 4000); // refresh while open — near-live view
    return () => clearInterval(t);
  });
  let hover = $state<{ name: string; x: number; y: number } | null>(null);
  let q = $state("");
  let planEl: HTMLDivElement;

  // Anchored-popover style: shifting by the anchor's own fraction keeps the card
  // fully inside the plan at any plan size (the plan clips overflow).
  function aPos(x: number, y: number): string {
    return `left:${x}%; top:${y}%; transform: translate(-${x}%, -${y}%)`;
  }

  const areas = $derived(devices.areas);
  const areaById = $derived(new Map(areas.map((a) => [a.id, a])));
  const floorAreas = $derived(areas.filter((a) => a.fp_poly && a.fp_floor === floor));
  const curFloor = $derived(floors.find((f) => f.key === floor));
  // Uploaded scaffold, served by the api; cache-bust by filename so a re-upload
  // shows at once. Reference only while editing — the plan itself renders vector.
  const floorImg = $derived(curFloor?.img_path ? `/api/floorplan/${curFloor.key}?v=${encodeURIComponent(curFloor.img_path)}` : "");
  // A floor drawn by hand needs no scaffold: borders (draft or saved) and traced rooms
  // ARE the plan. Without this the sample apartment kept showing under the real rooms.
  const hasOwnPlan = $derived(borderDraft.length > 0 || floorAreas.length > 0);

  // Night dims the plan: helper:daynight (Ecowitt-solar driven) → a soft dark-navy
  // overlay over the floor, below the markers so devices stay legible.
  const isNight = $derived(devices.byId["helper:daynight"]?.caps?.text?.value === "night");

  // Item derivation (grouping, labels, glows) lives in $lib/floorplanItems —
  // shared with the wall panel's read-only plan so both surfaces agree.
  const candidates = $derived(planCandidates());
  const items = $derived.by<PlaceItem[]>(() => buildPlanItems(candidates));

  const isPlaced = isPlacedAny;
  const itemPos = (it: PlaceItem): { x: number; y: number } | null => itemPosOn(it, floor);
  const placedItems = $derived(items.map((it) => ({ it, p: itemPos(it) })).filter((x) => x.p));
  // A "now playing" media marker with nothing playing hides in view mode (still draggable
  // in edit) — its icon shows only while audio/video is on.
  const mediaIdle = (it: PlaceItem): boolean =>
    it.members[0]?.fpStyle?.effect === "media" && it.members[0]?.caps["enum"]?.value === "off";

  // ── ROOM sensor labels: one aggregated label per area on this floor. Its sensors
  //    are the entities assigned to the area that report a numeric reading; per
  //    capability we show the mean. Anchored at the area's fp_x/fp_y (draggable). ──
  type RoomLabel = RoomLabelItem;
  // Every label-eligible area on this floor (incl. ones the user hid → the sidebar
  // lists them so a wrongly-matched label can be removed or a removed one re-added).
  const roomLabelAreas = $derived.by<RoomLabel[]>(() => buildRoomLabels(floor));
  const roomLabels = $derived(roomLabelAreas.filter((r) => !r.off)); // shown on the plan
  // Keep the open room panel in sync with live values (re-derive by area id).
  const roomOpenLive = $derived(roomOpen ? roomLabels.find((r) => r.area.id === roomOpen!.area.id) ?? null : null);

  // ── room label: tap opens the panel; edit-drag repositions the area anchor ──
  let roomDrag: { id: number; moved: boolean; x0: number; y0: number } | null = null;
  function onRoomDown(e: PointerEvent, rl: RoomLabel) {
    e.stopPropagation();
    roomDrag = { id: rl.area.id, moved: false, x0: e.clientX, y0: e.clientY };
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  }
  function onRoomMove(e: PointerEvent, rl: RoomLabel) {
    if (!roomDrag || roomDrag.id !== rl.area.id || !edit) return;
    if (!roomDrag.moved && Math.hypot(e.clientX - roomDrag.x0, e.clientY - roomDrag.y0) > 4) roomDrag.moved = true;
    if (!roomDrag.moved) return;
    const { x, y } = coords(e);
    const a = devices.areas.find((ar) => ar.id === rl.area.id);
    if (a) { a.fp_x = x; a.fp_y = y; } // optimistic; persisted on up
  }
  async function onRoomUp(e: PointerEvent, rl: RoomLabel) {
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
    const d = roomDrag;
    roomDrag = null;
    if (!d) return;
    if (d.moved) {
      const a = devices.areas.find((ar) => ar.id === rl.area.id);
      if (a) await devices.placeArea(a.id, floor, a.fp_x as number, a.fp_y as number);
    } else {
      roomOpen = roomOpen?.area.id === rl.area.id ? null : rl; // tap toggles the panel
    }
  }
  // Open history for a room reading (its included sensors → mean-over-time).
  function openRoomHistory(rl: RoomLabel, rc: RoomCap) {
    hist = {
      readings: rc.readings.filter((r) => r.included).map((r) => ({ entityId: r.entityId, cap: rc.cap })),
      cap: rc.cap, name: devices.roomLabel(rl.area), x: rl.x, y: rl.y,
    };
  }
  // ── room config: hide/show a capability, exclude/include a sensor from the mean ──
  function normalizeConfig(c: SensorConfig): SensorConfig | null {
    const out: SensorConfig = {};
    if (c.hidden?.length) out.hidden = c.hidden;
    if (c.excluded?.length) out.excluded = c.excluded;
    if (c.included?.length) out.included = c.included;
    if (c.order?.length) out.order = c.order;
    if (c.off !== undefined) out.off = c.off; // explicit boolean (may override a kind default)
    return Object.keys(out).length ? out : null;
  }
  // Remove a wrongly-matched room label from the plan, or re-add a removed one. Stores
  // the flag only when it OVERRIDES the kind default (stairs default off, else on).
  async function toggleRoomLabel(area: Area, off: boolean) {
    if (off && roomOpen?.area.id === area.id) roomOpen = null;
    const c = { ...(area.sensor_config ?? {}) };
    if (off === (area.kind === "stairs")) delete c.off; else c.off = off;
    await devices.setAreaSensorConfig(area.id, normalizeConfig(c));
  }
  // Drag & drop the value rows to reorder them; the top one becomes the plan headline.
  let dragCap = $state<string | null>(null);
  async function reorderCap(rl: RoomLabel, from: string, to: string) {
    if (from === to) return;
    const order = rl.caps.map((c) => c.cap);      // current (sorted) order
    order.splice(order.indexOf(to), 0, order.splice(order.indexOf(from), 1)[0]);
    await devices.setAreaSensorConfig(rl.area.id, normalizeConfig({ ...(rl.area.sensor_config ?? {}), order }));
  }
  async function toggleCap(area: Area, cap: string) {
    const c: SensorConfig = { ...(area.sensor_config ?? {}) };
    const s = new Set(c.hidden ?? []);
    s.has(cap) ? s.delete(cap) : s.add(cap);
    c.hidden = [...s];
    await devices.setAreaSensorConfig(area.id, normalizeConfig(c));
  }
  // Toggle a reading in/out of the mean, storing the MINIMAL override: an override is
  // kept only when the wanted state differs from the type default (pure=in, thermostat
  // =out-unless-sole). Default-in turned off → `excluded`; default-out turned on →
  // `included`; matching the default drops the override from both lists.
  async function toggleSensor(rc: RoomCap, area: Area, entityId: string) {
    const r = rc.readings.find((x) => x.entityId === entityId);
    if (!r) return;
    const want = !r.included;
    const dflt = readingDefaultIn(rc, entityId);
    const k = `${entityId}:${rc.cap}`;
    const c: SensorConfig = { ...(area.sensor_config ?? {}) };
    const excl = new Set(c.excluded ?? []);
    const incl = new Set(c.included ?? []);
    excl.delete(k); incl.delete(k);
    if (want !== dflt) (dflt ? excl : incl).add(k); // override needed only vs the default
    c.excluded = [...excl];
    c.included = [...incl];
    await devices.setAreaSensorConfig(area.id, normalizeConfig(c));
  }

  const groups = $derived.by(() => {
    const s = q.trim().toLowerCase();
    const unplaced = items.filter((it) => !isPlaced(it)).filter((it) => !s || it.name.toLowerCase().includes(s));
    const m = new Map<number | null, PlaceItem[]>();
    for (const it of unplaced) { const a = m.get(it.areaId); if (a) a.push(it); else m.set(it.areaId, [it]); }
    return [...m.entries()]
      .map(([areaId, its]) => ({ areaId, name: devices.areaName(areaId), items: its.sort((a, b) => a.name.localeCompare(b.name, "hr")) }))
      .sort((a, b) => (a.areaId === null ? 1 : b.areaId === null ? -1 : a.name.localeCompare(b.name, "hr")));
  });
  const unplacedCount = $derived(groups.reduce((n, g) => n + g.items.length, 0));

  // ── glow: each lit LIGHT contributes a shape (own radius, else its room polygon) ──
  const glows = $derived(buildGlows(placedItems, floorAreas, areaById, floor));

  // ── replay: every entity this plan renders, which is exactly the set the bundle
  //    is built for. The night overlay is part of how the house looked, so the
  //    day/night helper rides along. ──
  const replayEntities = $derived.by<string[]>(() => {
    const ids = new Set<string>(["helper:daynight"]);
    for (const { it } of placedItems) for (const m of it.members) ids.add(m.entityId);
    for (const rl of roomLabelAreas) for (const s of rl.sensors) ids.add(s.entityId);
    return [...ids];
  });

  async function toggleReplay(): Promise<void> {
    if (replay.active || replay.loading || replay.error) { await replay.close(); return; }
    edit = false;
    selected = null; configItem = null; hist = null; camSnap = null; roomOpen = null; roomSel = null;
    await replay.open(replayEntities);
  }

  // Leaving the page must hand the store back to the live stream — otherwise the
  // whole app stays frozen at whatever instant the cursor was left on.
  onMount(() => () => { void replay.close(); });

  const clamp = (v: number): number => Math.max(0, Math.min(100, v));
  const snap = (v: number): number => clamp(Math.round(v / GRID) * GRID);
  // Sending the robot somewhere: arm a mode, then the next tap on the plan names the
  // place. Two modes, because a tap only means something once the robot has been told
  // where it stands — the dock is the other anchor and DIDA already knows that one.
  let sendMode = $state(false);
  let sendSpot = $state<{ x: number; y: number } | null>(null);   // where the drag began
  let sendTo = $state<{ x: number; y: number } | null>(null);     // and where it ended
  let sendDrawing = $state(false);
  let sendBusy = $state(false);
  let sendErr = $state<string | null>(null);
  const PATCH_M = 1.5;
  const vacuumEntity = $derived(devices.list.find((d) => "vacuum" in d.caps)?.entityId ?? null);
  // A tap on the plan means nothing to the robot until it has been told where it
  // stands once. The adapter publishes that as state, so the button can say so
  // instead of accepting a tap that quietly goes nowhere.
  const planLinked = $derived(
    devices.list.find((d) => d.entityId.endsWith(":plan_link"))?.caps["boolean"]?.value === true);
  // The robot's own map, laid over ours. Dragging the picture into place IS the
  // calibration — anchors and room-pointing were the earlier attempt, and neither
  // could show how wrong it was.
  let vacMap = $state<VacuumMap | null>(null);
  let vacShow = $state(false);
  let vacEdit = $state(false);
  let vacPlace = $state<VacuumPlacement>({ x: 15, y: 20, w: 70, rotation: 0, mirrored: false, opacity: 0.55 });
  // Scaling keeps the map's proportions on purpose: its width and height are real
  // metres of floor, and stretching one axis alone would make every translated tap
  // wrong by a little more the further it is from the middle.
  let vacScale: { d: number; w: number; cx: number; cy: number } | null = null;

  function vacCentre() {
    return { cx: vacPlace.x + vacPlace.w / 2, cy: vacPlace.y + vacHeight / 2 };
  }
  function vacGrabCorner(e: PointerEvent) {
    e.stopPropagation();
    const { cx, cy } = vacCentre();
    const p = rawXY(e);
    vacScale = { d: Math.hypot(p.x - cx, p.y - cy) || 1, w: vacPlace.w, cx, cy };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  let vacDrag: { x: number; y: number; px: number; py: number } | null = null;
  const robotBusy = $derived(["cleaning", "returning", "starting"].includes(
    String(devices.list.find((d) => "vacuum" in d.caps)?.caps["vacuum"]?.value ?? "")));
  // Which of the robot's maps belongs to the storey on screen. Its own arrangement
  // says so; a map nobody has placed yet is offered for placing, because that is the
  // only way it ever gets one.
  let vacPick = $state<string | null>(null);
  let vacLoading = $state(false);
  const vacFloorMap = $derived.by(() => {
    const maps = vacMap?.maps ?? [];
    if (!maps.length) return null;
    // The storey on screen decides. A hand-picked map only overrides that while it is
    // being laid down — otherwise switching floors would keep showing the one picked
    // for the floor before.
    const mine = maps.find((m) => m.placement?.floor === floor) ?? null;
    if (mine) return mine;
    if (vacEdit) return maps.find((m) => m.map_id === vacPick)
      ?? maps.find((m) => m.map_id === vacMap?.current) ?? maps[0];
    return null;
  });

  // Changing storey drops a hand-picked map and adopts whatever that floor holds.
  $effect(() => {
    const here = floor;
    const mine = (vacMap?.maps ?? []).find((m) => m.placement?.floor === here);
    untrack(() => {
      vacPick = null;
      const p = mine?.placement;
      // Assigned field by field on purpose: spreading the current value would READ
      // the state this effect writes, and an effect that feeds itself never settles.
      if (p) {
        vacPlace = { x: p.x, y: p.y, w: p.w, rotation: p.rotation ?? 0,
                     mirrored: !!p.mirrored, opacity: p.opacity ?? 0.55 };
      }
    });
  });

  // The picture the "has the robot been here" test reads follows the map on screen.
  $effect(() => {
    const shown = vacFloorMap;
    if (!shown) { vacSeen = null; return; }
    void readMapAlpha(shown);
  });
  /** The nearest quarter turn, plus `steps` of them. Anything stored off-square —
   *  from the days of a slider — is squared up by the first press. */
  function quarter(current: number, steps: number): number {
    return (((Math.round(current / 90) + steps) * 90) % 360 + 360) % 360;
  }
  const vacUnplaced = $derived((vacMap?.maps ?? []).filter((m) => !m.placement?.floor));
  const vacHeight = $derived(vacFloorMap ? vacPlace.w * (vacFloorMap.height / vacFloorMap.width) : 0);

  function vacScaleMove(e: PointerEvent) {
    if (!vacScale) return;
    const p = rawXY(e);
    const w = Math.max(5, Math.min(300, vacScale.w * (Math.hypot(p.x - vacScale.cx, p.y - vacScale.cy) / vacScale.d)));
    const h = w * (vacFloorMap ? vacFloorMap.height / vacFloorMap.width : 1);
    vacPlace = { ...vacPlace, w, x: vacScale.cx - w / 2, y: vacScale.cy - h / 2 };
  }
  // What the house measures if the picture is right: the map knows its own metres,
  // so the plan's width follows from how wide the picture was drawn. A number that
  // reads as 30 m says the scale is wrong before anything is ever sent.
  const planMetres = $derived(
    vacFloorMap ? (vacFloorMap.width * vacFloorMap.grid_mm / 1000) * (100 / vacPlace.w) : 0);
  // The robot's current map belongs to one storey. Looking at another, its picture
  // would sit over rooms it has never seen and every tap would translate into the
  // wrong floor — so it simply is not drawn there.


  // A click that never became a drag still means something: the smallest patch worth
  // sending, a metre and a half of floor, expressed in plan percent through the map's
  // own scale.
  const patchPct = $derived(vacFloorMap && vacPlace.w
    ? (1.5 / (vacFloorMap.width * vacFloorMap.grid_mm / 1000)) * vacPlace.w : 6);
  const tenth = (v: number): number => Math.round(v * 10) / 10;
  const sendBox = $derived.by(() => {
    if (!sendSpot) return null;
    const to = sendTo ?? sendSpot;
    const tiny = Math.abs(to.x - sendSpot.x) < 1 && Math.abs(to.y - sendSpot.y) < 1;
    if (tiny) {
      const h = patchPct / 2;
      return { x1: sendSpot.x - h, y1: sendSpot.y - h, x2: sendSpot.x + h, y2: sendSpot.y + h };
    }
    return { x1: Math.min(sendSpot.x, to.x), y1: Math.min(sendSpot.y, to.y),
             x2: Math.max(sendSpot.x, to.x), y2: Math.max(sendSpot.y, to.y) };
  });
  const boxMetres = $derived.by(() => {
    if (!sendBox || !vacFloorMap || !vacPlace.w) return null;
    const perPct = (vacFloorMap.width * vacFloorMap.grid_mm / 1000) / vacPlace.w;
    return { w: (sendBox.x2 - sendBox.x1) * perPct, h: (sendBox.y2 - sendBox.y1) * perPct };
  });

  // The map's own transparency says where the robot has been. Read once into a
  // canvas so a tap can be answered here instead of by a command that comes back
  // refused: picking a place the robot has never seen should look impossible, not
  // fail afterwards.
  let vacSeen: { data: Uint8ClampedArray; w: number; h: number } | null = null;

  async function loadVacMap() {
    vacLoading = true;
    try {
      vacMap = await api.vacuumMap();
      const mine = vacMap.maps.find((m) => m.placement?.floor === floor)
        ?? vacMap.maps.find((m) => m.map_id === vacMap!.current) ?? vacMap.maps[0];
      vacPick = mine?.map_id ?? null;
      if (mine?.placement) vacPlace = { ...vacPlace, ...mine.placement };
    } catch (e) {
      vacErr = errMsg(e);
    } finally {
      vacLoading = false;
    }
  }

  async function readMapAlpha(entry: VacuumMapEntry) {
    try {
      const img = new Image();
      img.src = `data:image/png;base64,${entry.png}`;
      await img.decode();
      const canvas = document.createElement("canvas");
      canvas.width = entry.width;
      canvas.height = entry.height;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      if (!ctx) return;
      ctx.drawImage(img, 0, 0);
      vacSeen = { data: ctx.getImageData(0, 0, entry.width, entry.height).data,
                  w: entry.width, h: entry.height };
    } catch {
      vacSeen = null;   // without it a tap simply cannot be pre-judged; the send still can
    }
  }

  // Where it is, where it has been, what it was sent to do — polled while it works.
  let vacLive = $state<VacuumLive | null>(null);
  let vacTimer: ReturnType<typeof setInterval> | null = null;

  /** The robot's millimetres → a place on the plan: the placement, forwards. */
  function toPlan(mm: [number, number]): { x: number; y: number } {
    if (!vacFloorMap) return { x: 0, y: 0 };
    const h = vacHeight;
    let u = (mm[0] - vacFloorMap.origin[0]) / (vacFloorMap.width * vacFloorMap.grid_mm);
    const v = (mm[1] - vacFloorMap.origin[1]) / (vacFloorMap.height * vacFloorMap.grid_mm);
    if (vacPlace.mirrored) u = 1 - u;
    const rx = (u - 0.5) * vacPlace.w, ry = (v - 0.5) * h;
    const a = (vacPlace.rotation * Math.PI) / 180;
    return {
      x: vacPlace.x + vacPlace.w / 2 + rx * Math.cos(a) - ry * Math.sin(a),
      y: vacPlace.y + h / 2 + rx * Math.sin(a) + ry * Math.cos(a),
    };
  }

  const trackPoints = $derived((vacLive?.track ?? []).map(toPlan)
    .map((p) => `${p.x},${p.y}`).join(" "));
  const robotAt = $derived(vacLive?.robot ? toPlan(vacLive.robot) : null);
  const liveAreas = $derived((vacLive?.areas ?? []).map((a) => {
    const c1 = toPlan([a[0], a[1]]), c2 = toPlan([a[2], a[3]]);
    return { x: Math.min(c1.x, c2.x), y: Math.min(c1.y, c2.y),
             w: Math.abs(c2.x - c1.x), h: Math.abs(c2.y - c1.y) };
  }));

  // Only while there is something to watch: a docked robot has nothing to report,
  // and a poll every couple of seconds forever is a poll nobody asked for.
  $effect(() => {
    const wanted = vacShow && (robotBusy || vacLive !== null);
    if (wanted && !vacTimer) {
      void refreshLive();
      vacTimer = setInterval(() => void refreshLive(), 2000);
    } else if (!wanted && vacTimer) {
      clearInterval(vacTimer);
      vacTimer = null;
    }
    return () => { if (vacTimer && !vacShow) { clearInterval(vacTimer); vacTimer = null; } };
  });

  async function refreshLive() {
    try {
      vacLive = await api.vacuumLive();
    } catch {
      /* the robot's own state already says whether it is reachable */
    }
  }

  /** Has the robot been at this spot on the plan? Undoes the placement, then reads
   *  the map's own alpha at that pixel. */
  function robotKnows(point: { x: number; y: number }): boolean {
    if (!vacFloorMap || !vacSeen || !vacFloorMap.placement) return false;
    const h = vacHeight;
    const cx = vacPlace.x + vacPlace.w / 2, cy = vacPlace.y + h / 2;
    const a = (-vacPlace.rotation * Math.PI) / 180;
    const dx = point.x - cx, dy = point.y - cy;
    const rx = dx * Math.cos(a) - dy * Math.sin(a);
    const ry = dx * Math.sin(a) + dy * Math.cos(a);
    let u = (rx + vacPlace.w / 2) / vacPlace.w;
    const v = (ry + h / 2) / h;
    if (vacPlace.mirrored) u = 1 - u;
    if (u < 0 || u > 1 || v < 0 || v > 1) return false;
    const px = Math.min(vacSeen.w - 1, Math.floor(u * vacSeen.w));
    const py = Math.min(vacSeen.h - 1, Math.floor(v * vacSeen.h));
    return vacSeen.data[(py * vacSeen.w + px) * 4 + 3] > 0;
  }
  // Judged at the middle of what was drawn: a corner may legitimately fall on a wall.
  const spotKnown = $derived(sendBox
    ? robotKnows({ x: (sendBox.x1 + sendBox.x2) / 2, y: (sendBox.y1 + sendBox.y2) / 2 }) : true);
  let vacErr = $state<string | null>(null);

  // When the robot STARTS working, show its map without being asked — that is when
  // knowing where it has been is worth a screen. Only on that transition: for as long
  // as it worked, this used to undo every attempt to turn the map off.
  let wasBusy = $state(false);
  $effect(() => {
    const busy = robotBusy;
    untrack(() => {
      if (busy && !wasBusy) { vacShow = true; if (!vacMap) void loadVacMap(); }
      wasBusy = busy;
    });
  });

  const vacDirty = $derived.by(() => {
    const p = vacFloorMap?.placement;
    if (p?.floor !== floor) return true;
    return vacPlace.x !== p.x || vacPlace.y !== p.y || vacPlace.w !== p.w
      || vacPlace.rotation !== (p.rotation ?? 0) || vacPlace.mirrored !== !!p.mirrored
      || vacPlace.opacity !== (p.opacity ?? 0.55);
  });
  let vacSaving = $state(false);
  async function saveVacPlacement() {
    vacSaving = true;
    try {
      await api.setVacuumPlacement({ ...vacPlace, adapter: "dreame",
                                     map_id: vacFloorMap?.map_id ?? "", floor });
      await loadVacMap();
      vacEdit = false;
      vacErr = null;
    } catch (e) {
      vacErr = errMsg(e);
    } finally {
      vacSaving = false;
    }
  }

  function vacDown(e: PointerEvent) {
    if (!vacEdit) return;
    const p = rawXY(e);
    vacDrag = { x: vacPlace.x, y: vacPlace.y, px: p.x, py: p.y };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function vacMove(e: PointerEvent) {
    if (!vacDrag) return;
    const p = rawXY(e);
    vacPlace = { ...vacPlace, x: vacDrag.x + (p.x - vacDrag.px), y: vacDrag.y + (p.y - vacDrag.py) };
  }
  function vacUp() { vacDrag = null; }

  async function sendToSpot() {
    if (!sendBox || !vacuumEntity) return;
    sendBusy = true;
    sendErr = null;
    try {
      await api.sendCommand({
        entity_id: vacuumEntity, capability: "vacuum",
        command: "clean_area",
        args: { x1: tenth(sendBox.x1), y1: tenth(sendBox.y1), x2: tenth(sendBox.x2), y2: tenth(sendBox.y2) },
      });
      sendMode = false;
      sendSpot = null;
      sendTo = null;
    } catch (e) {
      sendErr = errMsg(e);
    } finally {
      sendBusy = false;
    }
  }

  function rawXY(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const r = planEl.getBoundingClientRect();
    return { x: clamp(((e.clientX - r.left) / r.width) * 100), y: clamp(((e.clientY - r.top) / r.height) * 100) };
  }
  function coords(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const { x, y } = rawXY(e);
    return { x: snap(x), y: snap(y) };
  }

  const persist = (it: PlaceItem, x: number, y: number) => Promise.all(it.members.map((m) =>
    it.slot === "motion" ? devices.placeMotion(m.entityId, floor, x, y) : devices.place(m.entityId, floor, x, y)));
  const setLocal = (it: PlaceItem, x: number, y: number) => {
    for (const m of it.members) {
      if (it.slot === "motion") { m.fpMotionFloor = floor; m.fpMotionX = x; m.fpMotionY = y; }
      else { m.fpFloor = floor; m.fpX = x; m.fpY = y; }
    }
  };
  const clearItem = (it: PlaceItem) => Promise.all(it.members.map((m) =>
    it.slot === "motion" ? devices.placeMotion(m.entityId, null, null, null) : devices.place(m.entityId, null, null, null)));

  async function onPlaceClick(e: MouseEvent) {
    const it = items.find((x) => x.key === picking);
    const { x, y } = coords(e);
    if (it) await persist(it, x, y);
    picking = null;
  }

  onMount(async () => {
    try { floors = await api.listFloors(); } catch { floors = []; }
    if (!floors.some((f) => f.key === floor)) floor = sortedFloors[0]?.key ?? "";
    floorsLoaded = true;
  });

  // ── border draft: load / undo / save ──
  const snapB = (): Border[] => $state.snapshot(borderDraft) as Border[];
  function pushUndo(prev: Border[] = snapB()) { undoStack = [...undoStack.slice(-49), prev]; }
  function undo() {
    const prev = undoStack.at(-1);
    if (!prev) return;
    undoStack = undoStack.slice(0, -1);
    borderDraft = prev;
    selBorder = null; mergeFrom = null; mergePreview = null; roomSel = null;
  }
  function loadBorders() {
    const src = (curFloor?.borders ?? []).map((b) => [...b] as Border);
    borderDraft = src;
    borderSaved = JSON.stringify(src);
    undoStack = []; rooms = [];
    selBorder = null; addingBorder = false; drawBorder = null; snapDot = null;
    roomSel = null; mergeFrom = null; mergePreview = null;
  }
  // Load on floor change ONLY — the switch-marker drag and Save both rewrite `floors`,
  // and neither may reset an in-progress draft (hence the untrack).
  $effect(() => { floor; untrack(() => loadBorders()); });

  // Edit mode is a focused task: its toolbox docks to the bottom of a small screen,
  // so the shell's tab bar steps aside for the duration.
  $effect(() => {
    ui.focusMode = edit;
    return () => { ui.focusMode = false; };
  });

  async function detectBordersFn() {
    if (!floorImg || borderBusy) return;
    borderBusy = true;
    try {
      const b = (await api.detectBorders(floor)).borders;
      pushUndo();
      borderDraft = b;
      selBorder = null; mergeFrom = null; mergePreview = null; roomSel = null;
    } catch { /* keep current */ }
    finally { borderBusy = false; }
  }
  async function saveBordersFn() {
    const id = curFloor?.id;
    if (id == null || borderBusy || !bordersDirty) return;
    borderBusy = true;
    try {
      const f = await api.saveFloorBorders(id, snapB());
      floors = floors.map((x) => (x.id === id ? f : x));
      borderSaved = JSON.stringify(borderDraft);
    } catch { /* stays dirty */ }
    finally { borderBusy = false; }
  }
  function deleteBorder(i: number) {
    pushUndo();
    borderDraft = borderDraft.filter((_, k) => k !== i);
    selBorder = null;
  }

  // ── rooms derive live from the draft: every border edit shows its effect at once ──
  let deriveTimer: ReturnType<typeof setTimeout> | null = null;
  let deriveSeq = 0;
  $effect(() => {
    // Guard BEFORE reading borderDraft: in view mode this effect shouldn't
    // subscribe to the draft at all (it re-runs on edit/mode instead).
    if (!(edit && mode === "plan")) return;
    const json = JSON.stringify(borderDraft);
    if (deriveTimer) clearTimeout(deriveTimer);
    const seq = ++deriveSeq;
    deriveTimer = setTimeout(async () => {
      if (json === "[]") { rooms = []; return; }
      deriving = true;
      try {
        const r = (await api.roomsFromBorders(floor, JSON.parse(json))).rooms;
        if (seq === deriveSeq) rooms = r;
      } catch { /* keep last */ }
      finally { if (seq === deriveSeq) deriving = false; }
    }, 400);
    // Without this the last scheduled derive survives teardown: it fires after the
    // page is gone, calls the API and writes `rooms`/`deriving` on a dead component.
    return () => { if (deriveTimer) { clearTimeout(deriveTimer); deriveTimer = null; } };
  });

  // ── rooms as clickable items: derived candidates get numbers, matched areas their
  //    name; assigned areas without a matching derived room still render (and clear) ──
  const roomItems = $derived.by<RoomItem[]>(() => {
    const src = mergePreview ? mergePreview.rooms : rooms;
    const out: RoomItem[] = [];
    let n = 0;
    for (const poly of src) {
      const c = polygonCentroid(poly);
      const a = floorAreas.find((ar) => pointInPolygon(c.x, c.y, ar.fp_poly as FloorPoly));
      out.push({ poly, areaId: a?.id ?? null, cx: c.x, cy: c.y, n: a ? null : ++n });
    }
    const seen = new Set(out.map((r) => r.areaId));
    for (const a of floorAreas) {
      if (seen.has(a.id)) continue;
      const c = polygonCentroid(a.fp_poly as FloorPoly);
      out.push({ poly: a.fp_poly as FloorPoly, areaId: a.id, cx: c.x, cy: c.y, n: null });
    }
    return out;
  });
  // Areas not yet traced on THIS floor — the assignment targets, alphabetical.
  const assignableAreas = $derived(
    areas.filter((a) => !(a.fp_poly && a.fp_floor === floor))
      .toSorted((a, b) => devices.roomLabel(a).localeCompare(devices.roomLabel(b), "hr", { sensitivity: "base" })),
  );

  function onRoomClick(r: RoomItem) {
    if (mergePreview) return;                 // confirm or cancel first
    if (mergeFrom) {
      if (mergeFrom.cx === r.cx && mergeFrom.cy === r.cy) { mergeFrom = null; return; }
      void buildMergePreview(mergeFrom, r);
      return;
    }
    roomSel = r;
  }

  async function assignRoom(r: RoomItem, value: string) {
    let id: number;
    if (value === "__new__") {
      const area = await api.createArea(t("fp.detectedRoom", { n: devices.areas.length + 1 }));
      devices.areas = [...devices.areas, { ...area, fp_floor: null, fp_x: null, fp_y: null, fp_poly: null } as Area];
      id = area.id;
    } else if (value) {
      id = Number(value);
    } else return;
    await devices.placeArea(id, floor, r.cx, r.cy);
    await devices.setAreaPoly(id, r.poly as FloorPoly);
    roomSel = null;
  }
  async function clearArea(id: number) {
    await devices.setAreaPoly(id, null);
    await devices.placeArea(id, null, null, null);
    roomSel = null;
  }

  // ── merge = erase the border between two rooms (preview → confirm, all in the draft) ──
  function flashMergeMsg(msg: string) {
    mergeMsg = msg;
    if (mergeMsgTimer) clearTimeout(mergeMsgTimer);
    mergeMsgTimer = setTimeout(() => (mergeMsg = null), 3500);
  }
  async function buildMergePreview(a: RoomItem, b: RoomItem) {
    borderBusy = true;
    try {
      const res = await api.eraseBorder(floor, snapB(), [a.poly, b.poly]);
      if (!res.removed.length) { flashMergeMsg(t("fp.mergeNotAdjacent")); return; }
      const derived = (await api.roomsFromBorders(floor, res.borders)).rooms;
      mergePreview = { borders: res.borders, removed: res.removed, rooms: derived, from: a, toAreaId: b.areaId };
    } catch { /* nothing changes */ }
    finally { mergeFrom = null; borderBusy = false; }
  }
  async function confirmMerge() {
    const mp = mergePreview;
    if (!mp) return;
    pushUndo();
    borderDraft = mp.borders;
    rooms = mp.rooms;
    mergePreview = null;
    // The merged space keeps the FIRST room's area (else the neighbour's); a second
    // assignment covering the same space is cleared. Undo restores the borders only —
    // area polygons are explicit assignments, re-assign after an undo if needed.
    const target = mp.rooms.find((p) => pointInPolygon(mp.from.cx, mp.from.cy, p));
    const keepId = mp.from.areaId ?? mp.toAreaId;
    const dropId = mp.from.areaId != null && mp.toAreaId != null && mp.toAreaId !== mp.from.areaId ? mp.toAreaId : null;
    if (keepId != null && target) {
      const c = polygonCentroid(target);
      await devices.placeArea(keepId, floor, c.x, c.y);
      await devices.setAreaPoly(keepId, target as FloorPoly);
    }
    if (dropId != null) {
      await devices.setAreaPoly(dropId, null);
      await devices.placeArea(dropId, null, null, null);
    }
  }

  // ── border geometry snapping: ends bind to existing borders, junctions close exactly ──
  // Centrelines of every border except `exclude`: vertical (x, y-span) / horizontal.
  function borderLines(exclude = -1) {
    const v: { x: number; y0: number; y1: number }[] = [];
    const hz: { y: number; x0: number; x1: number }[] = [];
    borderDraft.forEach((b, i) => {
      if (i === exclude) return;
      const [x, y, w, h] = b;
      if (h >= w) v.push({ x: x + w / 2, y0: y, y1: y + h });
      else hz.push({ y: y + h / 2, x0: x, x1: x + w });
    });
    return { v, hz };
  }
  // A free point (draw anchor / draw end): border ENDS win, then border LINES per axis
  // (a T-junction lands ON the crossing border), then the 0.5 % grid.
  function snapPoint(px: number, py: number, exclude = -1): { x: number; y: number; snap: boolean } {
    const { v, hz } = borderLines(exclude);
    const ends: [number, number][] = [];
    for (const l of v) ends.push([l.x, l.y0], [l.x, l.y1]);
    for (const l of hz) ends.push([l.x0, l.y], [l.x1, l.y]);
    let ex: number | null = null, ey = 0, bd = SNAP_R;
    for (const [cx, cy] of ends) {
      const d = Math.hypot(cx - px, cy - py);
      if (d < bd) { bd = d; ex = cx; ey = cy; }
    }
    if (ex != null) return { x: ex, y: ey, snap: true };
    let lx: number | null = null, ly: number | null = null, dv = SNAP_R, dh = SNAP_R;
    for (const l of v) if (py > l.y0 - SNAP_R && py < l.y1 + SNAP_R) { const d = Math.abs(l.x - px); if (d < dv) { dv = d; lx = l.x; } }
    for (const l of hz) if (px > l.x0 - SNAP_R && px < l.x1 + SNAP_R) { const d = Math.abs(l.y - py); if (d < dh) { dh = d; ly = l.y; } }
    return { x: lx ?? snapHalf(px), y: ly ?? snapHalf(py), snap: lx != null || ly != null };
  }
  // One X coordinate (a moving edge / centreline): vertical centrelines (end ON a
  // crossing border) + horizontal borders' ends (colinear continuation), else grid.
  function snapCoordX(vx: number, exclude = -1): number {
    const { v, hz } = borderLines(exclude);
    let best = snapHalf(vx), bd = SNAP_R;
    for (const c of [...v.map((l) => l.x), ...hz.flatMap((l) => [l.x0, l.x1])]) {
      const d = Math.abs(c - vx);
      if (d < bd) { bd = d; best = c; }
    }
    return best;
  }
  function snapCoordY(vy: number, exclude = -1): number {
    const { v, hz } = borderLines(exclude);
    let best = snapHalf(vy), bd = SNAP_R;
    for (const c of [...hz.map((l) => l.y), ...v.flatMap((l) => [l.y0, l.y1])]) {
      const d = Math.abs(c - vy);
      if (d < bd) { bd = d; best = c; }
    }
    return best;
  }

  // ── border drag: move / edge-resize, snapped; the whole gesture is ONE undo step ──
  type BorderDrag = { i: number; kind: "move" | "n" | "s" | "e" | "w"; ox: number; oy: number; base: Border };
  let borderDrag: BorderDrag | null = null;
  let dragSnap: Border[] | null = null;   // pre-drag state → pushed to undo on real change
  function onBorderDown(e: PointerEvent, i: number, kind: BorderDrag["kind"]) {
    e.stopPropagation();
    selBorder = i;
    const { x, y } = rawXY(e);
    borderDrag = { i, kind, ox: x, oy: y, base: [...borderDraft[i]] as Border };
    dragSnap = snapB();
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  }
  function onBorderMove(e: PointerEvent) {
    if (!borderDrag) return;
    const { x, y } = rawXY(e);
    const dx = x - borderDrag.ox, dy = y - borderDrag.oy;
    const i = borderDrag.i, k = borderDrag.kind;
    let [nx, ny, nw, nh] = borderDrag.base;
    if (k === "move") {
      if (nh >= nw) { // vertical: snap the centreline into colinear runs
        nx = snapCoordX(nx + nw / 2 + dx, i) - nw / 2;
        ny = snapHalf(ny + dy);
      } else {
        ny = snapCoordY(ny + nh / 2 + dy, i) - nh / 2;
        nx = snapHalf(nx + dx);
      }
    } else if (k === "w") { const e0 = snapCoordX(nx + dx, i); nw = nx + nw - e0; nx = e0; }
    else if (k === "e") { nw = snapCoordX(nx + nw + dx, i) - nx; }
    else if (k === "n") { const e0 = snapCoordY(ny + dy, i); nh = ny + nh - e0; ny = e0; }
    else if (k === "s") { nh = snapCoordY(ny + nh + dy, i) - ny; }
    if (nw < 0) { nx += nw; nw = -nw; }
    if (nh < 0) { ny += nh; nh = -nh; }
    borderDraft[i] = [nx, ny, Math.max(0.5, nw), Math.max(0.5, nh)];
  }
  function onBorderUp(e: PointerEvent) {
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
    borderDrag = null;
    if (dragSnap && JSON.stringify(dragSnap) !== JSON.stringify(borderDraft)) pushUndo(dragSnap);
    dragSnap = null;
  }

  // add a border: drag from a (snapped) anchor; pure horizontal/vertical by dominant axis
  let drawAnchor: { x: number; y: number } | null = null;
  function onAddDown(e: PointerEvent) {
    if (!addingBorder) return;
    const { x, y } = rawXY(e);
    const p = snapPoint(x, y);
    drawAnchor = { x: p.x, y: p.y };
    drawBorder = [p.x, p.y, 0, 0];
    snapDot = p.snap ? { x: p.x, y: p.y } : null;
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  }
  function onAddMove(e: PointerEvent) {
    if (!drawAnchor) return;
    const { x, y } = rawXY(e);
    const p = snapPoint(x, y);
    const dx = p.x - drawAnchor.x, dy = p.y - drawAnchor.y;
    if (Math.abs(dx) >= Math.abs(dy)) {
      drawBorder = [Math.min(drawAnchor.x, p.x), drawAnchor.y - BORDER_T / 2, Math.abs(dx), BORDER_T];
      snapDot = p.snap ? { x: p.x, y: drawAnchor.y } : null;
    } else {
      drawBorder = [drawAnchor.x - BORDER_T / 2, Math.min(drawAnchor.y, p.y), BORDER_T, Math.abs(dy)];
      snapDot = p.snap ? { x: drawAnchor.x, y: p.y } : null;
    }
  }
  function onAddUp() {
    if (drawBorder && (drawBorder[2] > 1 || drawBorder[3] > 1)) {
      pushUndo();
      borderDraft = [...borderDraft, drawBorder];
      selBorder = borderDraft.length - 1;
    }
    drawBorder = null; drawAnchor = null; addingBorder = false; snapDot = null;
  }
  // edge-midpoint resize handles for the selected border
  function handlesFor(b: Border) {
    const [x, y, w, h] = b;
    return [
      { k: "n" as const, x: x + w / 2, y },
      { k: "s" as const, x: x + w / 2, y: y + h },
      { k: "w" as const, x, y: y + h / 2 },
      { k: "e" as const, x: x + w, y: y + h / 2 },
    ];
  }
  function onPlanKey(e: KeyboardEvent) {
    const el = e.target as HTMLElement | null;
    if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
    if (e.key === "Escape") {
      // Close the topmost open popover first (works in view mode too).
      if (camSnap) { camSnap = null; return; }
      if (hist) { hist = null; return; }
      if (roomOpen) { roomOpen = null; return; }
      if (selected) { selected = null; return; }
      if (configItem) { configItem = null; return; }
      if (!edit) return;
      mergePreview = null; mergeFrom = null; roomSel = null;
      addingBorder = false; drawBorder = null; snapDot = null;
      return;
    }
    if (!edit || mode !== "plan") return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
    if (selBorder != null && e.key === "Delete") { e.preventDefault(); deleteBorder(selBorder); }
  }

  // ── floor switch: with >1 floor a stairs marker on the plan replaces floor tabs.
  //    Per-client UI navigation, deliberately NOT an entity (two viewers browse
  //    independently). ONE control at ONE position shared by every floor — the
  //    stairwell sits in the same plan spot on all levels; it shows the CURRENT
  //    floor (the value) and a tap cycles it. Edit-drag repositions (persisted). ──
  const SWITCH_DEF = { x: 93, y: 93 };
  const switchPos = $derived.by(() => {
    const f = sortedFloors.find((x) => x.switch_x != null && x.switch_y != null);
    return f ? { x: f.switch_x as number, y: f.switch_y as number } : SWITCH_DEF;
  });

  // ── one HOME-WIDE size multiplier for every marker (composes with per-device scale);
  //    stored on every floors row (like switchPos) so any signed-in user reads it ──
  const markerScale = $derived(sortedFloors.find((f) => f.marker_scale != null)?.marker_scale ?? 1);
  function setMarkerScaleLocal(v: number) {
    floors = floors.map((f) => ({ ...f, marker_scale: v }));
  }
  async function saveMarkerScale(v: number) {
    setMarkerScaleLocal(v);
    try { await Promise.all(floors.map((f) => api.updateFloor(f.id, { marker_scale: v }))); }
    catch { /* re-syncs on next load */ }
  }
  // Reset every per-device size override so all markers render at the SAME size — just
  // the global scale (which new markers already inherit). Dedupe by style entity.
  async function uniformSize() {
    const seen = new Set<string>();
    const jobs: Promise<void>[] = [];
    for (const it of items) {
      if (it.scale !== 1 && !seen.has(it.styleId)) { seen.add(it.styleId); jobs.push(devices.setStyle(it.styleId, { scale: null })); }
    }
    await Promise.all(jobs);
  }
  const anyResized = $derived(items.some((it) => it.scale !== 1));
  const nextFloor = $derived.by(() => {
    if (sortedFloors.length < 2) return null;
    const i = sortedFloors.findIndex((f) => f.key === floor);
    return sortedFloors[(i + 1) % sortedFloors.length];
  });
  let swDrag: { moved: boolean } | null = null;
  function switchFloor() {
    if (!nextFloor) return;
    floor = nextFloor.key;
    selected = null; configItem = null; hist = null; camSnap = null; roomOpen = null; picking = null; hover = null;
  }
  function onSwitchDown(e: PointerEvent) {
    e.stopPropagation();
    swDrag = { moved: false };
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  }
  function onSwitchMove(e: PointerEvent) {
    if (!swDrag || !edit) return;
    const { x, y } = rawXY(e);
    swDrag.moved = true;
    const sx = snapHalf(x), sy = snapHalf(y);
    floors = floors.map((f) => ({ ...f, switch_x: sx, switch_y: sy }));
  }
  async function onSwitchUp(e: PointerEvent) {
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
    const d = swDrag;
    swDrag = null;
    if (!d) return;
    if (d.moved) {
      const p = switchPos;
      try { await Promise.all(floors.map((f) => api.updateFloor(f.id, { switch_x: p.x, switch_y: p.y }))); }
      catch { /* position re-syncs on next load */ }
    } else {
      switchFloor();
    }
  }

  // ── marker interaction: view = tap/long-press; edit = drag + tap opens radius ──
  const LONG_MS = 450, MOVE_CANCEL = 8;
  let dragKey = $state<string | null>(null);
  let moved = false;
  let pressTimer: ReturnType<typeof setTimeout> | null = null;
  let longFired = false, pressX = 0, pressY = 0;
  const clearPress = () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } };

  function onDown(e: PointerEvent, it: PlaceItem) {
    if (edit) {
      e.stopPropagation();
      dragKey = it.key; moved = false; pressX = e.clientX; pressY = e.clientY;
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      return;
    }
    longFired = false;
    pressX = e.clientX; pressY = e.clientY;   // always — onUp tells a tap from a stray drag
    if (isPlug(it)) return; // a plug opens its detail popover on tap — no long-press
    // A camera zone's dot: tap = snapshot (what it sees NOW), hold = the zone's
    // presence history. Sensor-only dots open history on plain tap (in onUp).
    if (it.slot === "motion") {
      const pc = presenceCapOf(it);
      if (pc && cameraFor(it)) {
        clearPress();
        pressTimer = setTimeout(() => { longFired = true; openValueHistory(it, pc.entityId, pc.cap); }, LONG_MS);
      }
      return;
    }
    // A mower's main datum is its run-state timeline (mowing/docked) — hold on the
    // marker goes straight there; tap still opens the card (start/pause/dock).
    const mowerM = it.members.find((m) => "mower" in m.caps || "vacuum" in m.caps);
    if (mowerM) {
      clearPress();
      pressTimer = setTimeout(() => { longFired = true; openValueHistory(it, mowerM.entityId, "mower" in mowerM.caps ? "mower" : "vacuum"); }, LONG_MS);
      return;
    }
    if (!it.toggle) return;
    clearPress();
    // Tap already acts (toggle), so hold is the inspection gesture: a plain switch
    // goes STRAIGHT to its on/off timeline; a multi-field device opens its card
    // (each value there is its own hold-for-history target).
    pressTimer = setTimeout(() => {
      longFired = true;
      if (it.hasExtra) void openControls(it);
      else openValueHistory(it, it.toggle!.entityId, "on_off");
    }, LONG_MS);
  }
  function onMove(e: PointerEvent, it: PlaceItem) {
    if (edit) {
      if (dragKey !== it.key) return;
      moved = true;
      const { x, y } = coords(e);
      setLocal(it, x, y);
      return;
    }
    if (pressTimer && Math.hypot(e.clientX - pressX, e.clientY - pressY) > MOVE_CANCEL) clearPress();
  }
  async function onUp(e: PointerEvent, it: PlaceItem) {
    if (edit) {
      if (dragKey !== it.key) return;
      dragKey = null;
      (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
      // A near-still press = a tap → open the config panel; a real drag = reposition.
      if (Math.hypot(e.clientX - pressX, e.clientY - pressY) < 6) { await openConfig(it); return; }
      const p = itemPos(it);
      if (p) await persist(it, p.x, p.y);
      return;
    }
    clearPress();
    if (longFired) { longFired = false; return; }
    if (Math.hypot(e.clientX - pressX, e.clientY - pressY) > 8) return; // a drag is not a tap
    if (it.slot === "motion" && !isPlug(it)) {
      // A camera zone's dot: tap shows what the camera sees RIGHT NOW (snapshot;
      // its occupancy history is on hold). A sensor-only dot (mmWave/PIR) has no
      // picture — tap goes straight to its presence history instead.
      const cam = cameraFor(it);
      const p = itemPos(it);
      if (cam && p) { camTick += 1; camSnap = { ...cam, x: p.x, y: p.y }; return; }
      const pc = presenceCapOf(it);
      if (pc) openValueHistory(it, pc.entityId, pc.cap);
      return;
    }
    if (isSensorLabel(it) && !isPlug(it)) return; // a readout's values handle their own taps (→ history)
    await shortTap(it);
  }
  async function openControls(it: PlaceItem) {
    if (replay.active) return; // a past instant is on screen — nothing here is commandable
    const cs = it.toggle?.caps["on_off"];
    if (auth.canControl && it.toggle && it.toggle.reachable === false) {
      toasts.error(t("card.unreachableCmd", { name: it.name }));
    } else if (auth.canControl && it.toggle && cs?.value !== true) {
      if (cs) cs.value = true;
      try { await api.sendCommand({ entity_id: it.toggle.entityId, capability: "on_off", command: "turn_on" }); } catch { /* corrects via WS */ }
    }
    selected = it;
  }
  // Hold on ONE value inside the open control popover → that value's history,
  // as a focused popover over the card (the card itself never stacks charts).
  function openValueHistory(it: PlaceItem, entityId: string, cap: string) {
    const p = itemPos(it);
    if (!p) return;
    hist = { readings: [{ entityId, cap }], cap, name: it.name, x: p.x, y: p.y };
  }

  // A vision marker maps to its camera by entity naming: baba:<cam>:zone:<z> →
  // baba:<cam> (the zone's dot is derived FROM that camera's picture). Non-camera
  // presence dots (mmWave) resolve to nothing — they have no picture to show.
  function cameraFor(it: PlaceItem): { eid: string; name: string } | null {
    for (const m of it.members) {
      const eid = m.entityId.split(":zone:")[0];
      const cam = devices.byId[eid];
      if (cam && "camera" in cam.caps) return { eid, name: cam.name };
    }
    return null;
  }

  // The presence channel a dot's history should chart, by the same precedence the
  // pip uses (person_count ▸ occupancy ▸ motion — considered verdicts over raw latches).
  function presenceCapOf(it: PlaceItem): { entityId: string; cap: string } | null {
    for (const cap of ["person_count", "occupancy", "motion"]) {
      for (const m of it.members) {
        if (cap in m.caps && !m.hiddenCaps.includes(cap)) return { entityId: m.entityId, cap };
      }
    }
    return null;
  }
  // Edit-mode tap: open the icon/size/glow panel; turn a light on so glow previews.
  async function openConfig(it: PlaceItem) {
    configItem = it;
    const cs = it.toggle?.caps["on_off"];
    if (it.type === "light" && it.toggle && cs?.value !== true) {
      if (cs) cs.value = true;
      try { await api.sendCommand({ entity_id: it.toggle.entityId, capability: "on_off", command: "turn_on" }); } catch { /* corrects via WS */ }
    }
  }
  async function shortTap(it: PlaceItem) {
    if (replay.active) return;
    // A now-playing media marker is read-only (the automation drives it) — a tap shows
    // its history, never a control menu.
    const m0 = it.members[0];
    if (m0?.fpStyle?.effect === "media") { openValueHistory(it, m0.entityId, "enum"); return; }
    if (opensControls(it)) { selected = selected?.key === it.key ? null : it; return; } // reveal controls/readings, don't toggle
    if (it.toggle && auth.canControl) {
      // An unreachable device cannot take the command — say so instead of
      // optimistically painting a state the mesh cannot deliver.
      if (it.toggle.reachable === false) {
        toasts.error(t("card.unreachableCmd", { name: it.name }));
        return;
      }
      const cs = it.toggle.caps["on_off"];
      const cur = cs?.value === true;
      if (cs) cs.value = !cur;
      try { await api.sendCommand({ entity_id: it.toggle.entityId, capability: "on_off", command: "toggle" }); } catch { if (cs) cs.value = cur; }
    } else {
      selected = selected?.key === it.key ? null : it;
    }
  }

  // Config panel actions (icon / size / glow), all keyed to the marker's style entity.
  const radiusOf = (it: PlaceItem | null): number => it?.toggle?.fpGlow?.r ?? 0;
  const setRadius = (it: PlaceItem, r: number) => it.toggle && devices.setGlow(it.toggle.entityId, { r });
  const useRoom = (it: PlaceItem) => it.toggle && devices.setGlow(it.toggle.entityId, null);
  const setIcon = (it: PlaceItem, icon: string | null) => devices.setStyle(it.styleId, { icon });
  const setScale = (it: PlaceItem, scale: number) => devices.setStyle(it.styleId, { scale });
  const setTint = (it: PlaceItem, color: string | null) => devices.setStyle(it.styleId, { color });

  // ── hover: show the room name over its space, hidden near a marker ──
  function onPlanHover(e: PointerEvent) {
    if (edit) { hover = null; return; }
    const { x, y } = rawXY(e);
    const near = placedItems.some(({ p }) => p && Math.hypot(p.x - x, p.y - y) < 5);
    if (near) { hover = null; return; }
    const a = floorAreas.find((ar) => ar.fp_poly && pointInPolygon(x, y, ar.fp_poly as FloorPoly));
    hover = a ? { name: devices.roomLabel(a), x, y } : null;
  }
</script>

<svelte:head><title>{t("nav.floorplan")}</title></svelte:head>

<PageActions>
  {#if !edit}
    <Button selected={replay.active} onclick={toggleReplay} disabled={replay.loading || replayEntities.length === 0} title={replayEntities.length === 0 ? t("replay.noEntities") : undefined}>{t("replay.open")}</Button>
  {/if}
  {#if auth.isAdmin && !replay.active}
    <Button selected={vacShow} onclick={async () => {
        vacShow = !vacShow;
        if (vacShow && !vacMap) await loadVacMap();
        // Never laid down before? Then showing it and adjusting it are the same act.
        if (vacShow && vacMap && !vacMap.placement) vacEdit = true;
        if (!vacShow) vacEdit = false;
      }}>{t("fp.robotMap")}</Button>
    {#if vacShow}
      <Button selected={vacEdit} onclick={() => (vacEdit = !vacEdit)}>{t("fp.adjustMap")}</Button>
    {/if}
    <Button selected={edit} onclick={() => { edit = !edit; picking = null; selected = null; configItem = null; hist = null; camSnap = null; roomOpen = null; roomSel = null; mergeFrom = null; mergePreview = null; selBorder = null; addingBorder = false; drawBorder = null; snapDot = null; }}>{edit ? t("fp.done") : t("fp.arrange")}</Button>
  {/if}
</PageActions>

{#if replay.active || replay.loading || replay.error}
  <div class="mb-3">
    <FloorReplay onclose={() => { void replay.close(); }} />
  </div>
{/if}

<!-- shared popover/bottom-sheet content: anchored card on desktop, sheet on a phone -->
{#snippet roomOptions(r: RoomItem)}
  <p class="mb-2 truncate pr-7 text-m font-semibold">{r.areaId != null ? devices.areaName(r.areaId) : t("fp.areaN", { n: r.n ?? 0 })}</p>
  {#if r.areaId == null}
    <select value="" onchange={(e) => assignRoom(r, e.currentTarget.value)}
      class="mb-2 w-full">
      <option value="">{t("fp.assignTo")}</option>
      <option value="__new__">{t("fp.newRoom")}</option>
      {#each assignableAreas as a (a.id)}
        <option value={a.id}>{devices.roomLabel(a)}</option>
      {/each}
    </select>
  {:else}
    <div class="mb-2 *:w-full"><Button size="small" tone="danger" onclick={() => r.areaId != null && clearArea(r.areaId)}>{t("fp.clearArea")}</Button></div>
  {/if}
  <div class="*:w-full"><Button size="small" onclick={() => { mergeFrom = r; roomSel = null; }}>{t("fp.mergeNeighbor")}</Button></div>
{/snippet}

<!-- Expanded room label: every reading of the room (mean per capability). View mode →
     each value opens its history; edit mode → toggle a capability, or a single sensor
     in/out of the mean when a room has more than one for the same thing. -->
{#snippet roomPanel(rl: RoomLabel)}
  {@const unit = (cap: string): string => (cap === "temperature" ? "°" : (capMeta(cap).unit ?? ""))}
  <div class="mb-2 flex items-center gap-2">
    <p class="min-w-0 flex-1 truncate pr-6 text-m font-semibold">{devices.roomLabel(rl.area)}</p>
  </div>
  <div class="flex flex-col gap-1">
    {#each (edit ? rl.caps : rl.caps.filter((c) => !c.hidden && c.mean != null)) as rc (rc.cap)}
      <!-- svelte-ignore a11y_no_static_element_interactions -- drag a capability chip onto the plan, edit mode only; positioning by pointer has no keyboard equivalent -->
      <div class="flex items-center gap-2 rounded text-m {dragCap === rc.cap ? 'opacity-40' : ''}"
        draggable={edit} ondragstart={() => (dragCap = rc.cap)} ondragend={() => (dragCap = null)}
        ondragover={(e) => { if (edit && dragCap) e.preventDefault(); }}
        ondrop={(e) => { if (edit && dragCap) { e.preventDefault(); reorderCap(rl, dragCap, rc.cap); dragCap = null; } }}>
        {#if edit}
          <span class="shrink-0 cursor-grab text-dida-text-faint" title={t("fp.reorder")}>⠿</span>
          <input type="checkbox" checked={!rc.hidden} onchange={() => toggleCap(rl.area, rc.cap)}
            title={t("fp.showValue")} class="shrink-0 accent-dida-accent" />
        {/if}
        <span class="min-w-0 flex-1 truncate text-s text-dida-text-muted">{capLabel(rc.cap)}</span>
        {#if rc.mean != null}
          <button type="button" onclick={() => openRoomHistory(rl, rc)}
            class="shrink-0 font-semibold tabular-nums {stateColor(rc.cap, rc.mean)} hover:underline">
            {formatValue(rc.cap, rc.mean)}<span class="opacity-60">{unit(rc.cap)}</span></button>
        {:else}
          <span class="shrink-0 text-s text-dida-text-faint">—</span>
        {/if}
      </div>
      {#if edit && rc.readings.length > 1}
        <!-- several sources for this reading → pick which feed the mean. A thermostat
             (AC/TRV, ⚙) is off by default — include it if its reading is good ambient. -->
        <div class="mb-1 ml-5 flex flex-col gap-0.5 border-l border-dida-border pl-2">
          {#each rc.readings as r (r.entityId)}
            <label class="flex items-center gap-2 text-xs text-dida-text-muted">
              <input type="checkbox" checked={r.included} onchange={() => toggleSensor(rc, rl.area, r.entityId)} class="shrink-0 accent-dida-accent" />
              <span class="min-w-0 flex-1 truncate">{#if r.secondary}<span class="text-dida-text-faint" title={t("fp.thermostat")}>⚙ </span>{/if}{r.name}</span>
              <span class="shrink-0 tabular-nums {stateColor(rc.cap, r.value)}">{formatValue(rc.cap, r.value)}{unit(rc.cap)}</span>
            </label>
          {/each}
        </div>
      {/if}
    {/each}
  </div>
  {#if edit}
    <div class="mt-2 *:w-full"><Button size="small" tone="danger" onclick={() => toggleRoomLabel(rl.area, true)}>{t("fp.removeLabel")}</Button></div>
  {/if}
{/snippet}

{#snippet devConfig(ci: PlaceItem)}
  <!-- `ci` is the PlaceItem snapshot captured when the panel opened; a pick rebuilds
       the items but not this reference, so selection/tint/scale read the LIVE store
       style — otherwise the grid keeps highlighting the pre-edit icon. -->
  {@const live = devices.byId[ci.styleId]?.fpStyle}
  <p class="mb-2 truncate text-m font-semibold">{ci.name}</p>

  <p class="mb-1 text-xs font-medium uppercase tracking-wide text-dida-text-muted">{t("fp.icon")}</p>
  <div class="grid max-h-36 grid-cols-6 gap-1 overflow-auto pr-1">
    <button type="button" onclick={() => setIcon(ci, null)} title={t("fp.iconAuto")}
      class="grid aspect-square place-items-center rounded border text-2xs {ci.styleId && !live?.icon ? 'border-dida-accent text-dida-accent' : 'border-dida-border text-dida-text-muted hover:border-dida-accent'}">A</button>
    {#each ICON_LIST as ic (ic)}
      <button type="button" onclick={() => setIcon(ci, ic)} title={ic}
        class="grid aspect-square place-items-center rounded border {(live?.icon ?? ci.icon) === ic ? 'border-dida-accent text-dida-accent' : 'border-dida-border text-dida-text-muted hover:border-dida-accent'}">
        <DeviceIcon type={ic} class="size-4" />
      </button>
    {/each}
  </div>

  <p class="mb-1 mt-3 text-xs font-medium uppercase tracking-wide text-dida-text-muted">{t("fp.size")}</p>
  <input type="range" min="0.6" max="2" step="0.1" value={live?.scale ?? ci.scale}
    oninput={(e) => setScale(ci, Number(e.currentTarget.value))} class="w-full" />

  <p class="mb-1 mt-3 text-xs font-medium uppercase tracking-wide text-dida-text-muted">{t("fp.tint")}</p>
  <div class="flex items-center gap-2">
    <input type="color" value={live?.color ?? "#0ea5b9"}
      oninput={(e) => setTint(ci, e.currentTarget.value)}
      class="h-7 w-10 cursor-pointer rounded border border-dida-border bg-dida-panel-2" aria-label={t("fp.tint")} />
    <div class="flex-1 *:w-full"><Button size="small" selected={!live?.color} onclick={() => setTint(ci, null)}>{t("fp.tintNone")}</Button></div>
  </div>

  {#if ci.type === "light"}
    <p class="mb-1 mt-3 text-xs font-medium uppercase tracking-wide text-dida-text-muted">{t("fp.glowRadius")}</p>
    <input type="range" min="3" max="30" step="0.5" value={radiusOf(ci) || DEFAULT_R}
      oninput={(e) => setRadius(ci, Number(e.currentTarget.value))} class="w-full" />
    <div class="mt-2 *:w-full"><Button size="small" selected={!radiusOf(ci)} onclick={() => useRoom(ci)}>{t("fp.useRoom")}</Button></div>
  {/if}
{/snippet}

<div class="flex flex-col gap-3 lg:flex-row">
  <!-- svelte-ignore a11y_no_static_element_interactions -- hover tracking on the plan surface: it drives the highlight, never an action -->
  <!-- isolate: the plan's own ladder (glow 1-16, markers 26-30, popovers 40) is a
       PRIVATE one. Without a stacking context of its own those numbers competed with
       the page's, and a marker at z-30 painted straight through the header's z-20
       menu — a light bulb sitting on top of "Log out". -->
  <div bind:this={planEl} onpointermove={onPlanHover} onpointerleave={() => (hover = null)}
    class="@container relative isolate mx-auto aspect-square w-full max-w-2xl select-none self-start overflow-hidden rounded-lg border border-dida-border bg-dida-panel max-sm:-mx-4 max-sm:w-[calc(100%+2rem)] max-sm:rounded-none max-sm:border-x-0">
    {#if floorImg}
      <img src={floorImg} alt={t("nav.floorplan")} class="pointer-events-none absolute inset-0 h-full w-full object-contain" draggable="false" />
    {:else if floorsLoaded && !hasOwnPlan}
      <!-- Only once floors are KNOWN to be missing a scaffold — rendering this while
           the list still loads flashed the sample apartment before every real plan. -->
      <FloorplanPlaceholder class="pointer-events-none absolute inset-0 h-full w-full opacity-70" />
      <p class="pointer-events-none absolute inset-x-0 bottom-3 z-[2] px-4 text-center text-xs text-dida-text-faint">
        {auth.isAdmin ? t("fp.samplePlanAdmin") : t("fp.samplePlan")}</p>
    {/if}

    <!-- Night: soft dark-navy wash over the floor (below markers at z-14+), so the plan
         reads as night while devices stay bright. Driven by helper:daynight. -->
    {#if isNight}
      <div class="pointer-events-none absolute inset-0 z-[3] bg-[#0a1526]/55 transition-opacity duration-[1500ms]"></div>
    {/if}

    <FloorGlow {glows} />

    {#if edit}
      <div class="pointer-events-none absolute inset-0 z-[1] opacity-40"
        style="background-image: linear-gradient(to right, rgba(120,140,160,.18) 1px, transparent 1px), linear-gradient(to bottom, rgba(120,140,160,.18) 1px, transparent 1px); background-size: {GRID}% {GRID}%"></div>
    {/if}

    <!-- room outlines while arranging devices (spatial context only) -->
    {#if edit && mode === "devices"}
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" class="pointer-events-none absolute inset-0 z-[2] h-full w-full">
        {#each floorAreas as a (a.id)}
          <polygon points={toPoints(a.fp_poly ?? [])} fill="rgba(45,212,191,.08)" stroke="rgba(45,212,191,.6)" stroke-width="0.3" />
        {/each}
      </svg>
    {/if}

    <!-- PLAN mode, layer 1 — rooms: every space the borders enclose, clickable. A click
         opens the options popover (assign / merge); while a merge is armed the next
         click picks the neighbour. Empty SVG space passes clicks through by design. -->
    {#if edit && mode === "plan"}
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" class="absolute inset-0 z-[14] h-full w-full">
        {#each roomItems as r (`${r.cx},${r.cy}`)}
          {@const armed = mergeFrom != null && mergeFrom.cx === r.cx && mergeFrom.cy === r.cy}
          {@const target = mergePreview != null && pointInPolygon(mergePreview.from.cx, mergePreview.from.cy, r.poly)}
          <!-- svelte-ignore a11y_click_events_have_key_events -- the polygon already carries role/aria-label; tabindex=-1 keeps it out of the tab order deliberately, so a keydown handler would be unreachable -->
          <polygon points={toPoints(r.poly)}
            fill={armed || target ? "rgba(45,212,191,.35)" : r.areaId != null ? "rgba(45,212,191,.10)" : "rgba(250,204,21,.08)"}
            stroke={armed || target ? "#2dd4bf" : r.areaId != null ? "rgba(45,212,191,.7)" : "#facc15"}
            stroke-width={armed || target ? "0.6" : "0.35"} stroke-dasharray={r.areaId != null ? "" : "1.4 1"}
            class="cursor-pointer" role="button" tabindex="-1"
            aria-label={r.areaId != null ? devices.areaName(r.areaId) : t("fp.areaN", { n: r.n ?? 0 })}
            onclick={() => onRoomClick(r)} />
          <text x={r.cx} y={r.cy} class="pointer-events-none"
            fill={armed || target ? "#2dd4bf" : r.areaId != null ? "rgba(45,212,191,.9)" : "#facc15"}
            font-size={r.areaId != null ? "2.4" : "3.2"} font-weight="700" text-anchor="middle" dominant-baseline="central">
            {r.areaId != null ? devices.areaName(r.areaId) : r.n}</text>
        {/each}
      </svg>
    {/if}

    {#if vacShow && !vacFloorMap && !vacEdit && !vacLoading}
      <p class="absolute left-1/2 top-14 z-30 -translate-x-1/2 rounded-lg border border-dida-border bg-dida-panel px-3 py-1.5 text-s text-dida-text-muted">
        {t("fp.noMapForFloor")}
      </p>
    {/if}
    {#if vacShow && vacFloorMap}
      <!-- The robot's own map. Under the markers on purpose: it is context, not the
           thing you reach for. In edit mode it takes the pointer so it can be dragged. -->
      <!-- svelte-ignore a11y_no_static_element_interactions -- laying a picture over a plan is a pointer act; the panel beside it carries the same controls as sliders -->
      <div style="left:{vacPlace.x}%;top:{vacPlace.y}%;width:{vacPlace.w}%;height:{vacHeight}%;
                  transform:rotate({vacPlace.rotation}deg)"
        onpointerdown={vacDown} onpointermove={(e) => { vacMove(e); vacScaleMove(e); }}
        onpointerup={() => { vacUp(); vacScale = null; }}
        class="absolute z-[6] origin-center {vacEdit ? 'cursor-move touch-none ring-2 ring-dida-accent' : 'pointer-events-none'}">
        <img src={`data:image/png;base64,${vacFloorMap.png}`} alt="" draggable="false"
          style="opacity:{vacPlace.opacity ?? 0.55};{vacPlace.mirrored ? 'transform:scaleX(-1)' : ''}"
          class="h-full w-full" />
        {#if vacEdit}
          {#each [["-left-2 -top-2", "nwse"], ["-right-2 -top-2", "nesw"], ["-left-2 -bottom-2", "nesw"], ["-right-2 -bottom-2", "nwse"]] as [at, dir] (at)}
            <button type="button" aria-label={t("fp.size")} onpointerdown={vacGrabCorner}
              style="cursor:{dir}-resize"
              class="absolute {at} size-4 touch-none rounded-full border-2 border-dida-bg bg-dida-accent shadow-lg"></button>
          {/each}
        {/if}
      </div>
    {/if}
    {#if vacShow && vacFloorMap && vacFloorMap.map_id === vacMap?.current && vacLive?.robot}
      <svg viewBox="0 0 100 100" preserveAspectRatio="none"
        class="pointer-events-none absolute inset-0 z-[7] h-full w-full">
        {#each liveAreas as a (a.x + ":" + a.y)}
          <rect x={a.x} y={a.y} width={a.w} height={a.h} vector-effect="non-scaling-stroke"
            fill="rgb(56 189 248 / 0.12)" stroke="rgb(56 189 248)" stroke-width="1.5" stroke-dasharray="3 2" />
        {/each}
        {#if trackPoints}
          <polyline points={trackPoints} fill="none" vector-effect="non-scaling-stroke"
            stroke="rgb(250 204 21)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity="0.9" />
        {/if}
      </svg>
      {#if robotAt}
        <div class="pointer-events-none absolute z-[8] -translate-x-1/2 -translate-y-1/2"
          style="left:{robotAt.x}%;top:{robotAt.y}%">
          <div class="size-3 rounded-full border-2 border-dida-bg bg-dida-warn ring-4 ring-dida-warn/25"></div>
        </div>
      {/if}
    {/if}
    {#if sendMode}
      <!-- svelte-ignore a11y_no_static_element_interactions -- drawing an area is a pointer path; the same job is reachable from the robot's own controls -->
      <div class="absolute inset-0 z-30 cursor-crosshair touch-none bg-dida-accent/5"
        onpointerdown={(e) => { sendSpot = rawXY(e); sendTo = null; sendDrawing = true; sendErr = null;
                                (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); }}
        onpointermove={(e) => { if (sendDrawing) sendTo = rawXY(e); }}
        onpointerup={() => (sendDrawing = false)}></div>
      {#if sendBox}
        <div class="pointer-events-none absolute z-40 border-2 border-dida-accent bg-dida-accent/25"
          style="left:{sendBox.x1}%;top:{sendBox.y1}%;width:{sendBox.x2 - sendBox.x1}%;height:{sendBox.y2 - sendBox.y1}%"></div>
        <div style={aPos(sendBox.x2, sendBox.y2)} class="absolute z-40 w-56 rounded-lg border border-dida-border bg-dida-panel p-3 text-s shadow-xl">
          <p class="mb-2 font-semibold">
            {boxMetres ? t("fp.cleanArea", { w: formatNumber(boxMetres.w, { maximumFractionDigits: 1 }), h: formatNumber(boxMetres.h, { maximumFractionDigits: 1 }) })
                       : t("fp.cleanHere", { m: PATCH_M })}
          </p>
          {#if !spotKnown}
            <p class="mb-2 text-dida-warn">{t("fp.robotDoesNotKnowThere")}</p>
          {/if}
          {#if sendErr}<p class="mb-2 text-dida-danger">{sendErr}</p>{/if}
          <div class="flex gap-2">
            <div class="flex-1 *:w-full"><Button size="small" tone="primary" onclick={sendToSpot} disabled={sendBusy || !spotKnown}>{t("fp.send")}</Button></div>
            <Button size="small" onclick={() => { sendSpot = null; sendTo = null; sendMode = false; }}>{t("fp.cancel")}</Button>
          </div>
        </div>
      {/if}
    {/if}
    {#if vacEdit && vacFloorMap}
      <div class="absolute bottom-2 left-2 z-40 w-64 rounded-lg border border-dida-border bg-dida-panel p-3 text-s shadow-xl">
        <p class="mb-2 font-semibold">{t("fp.layMapHelp")}</p>
        {#if (vacMap?.maps.length ?? 0) > 1}
          <label class="mb-2 flex items-center gap-2">
            <span class="w-14 shrink-0 text-dida-text-muted">{t("fp.whichMap")}</span>
            <select value={vacPick}
              onchange={(e) => { vacPick = e.currentTarget.value;
                                 const m = vacMap?.maps.find((x) => x.map_id === vacPick);
                                 if (m?.placement) vacPlace = { ...vacPlace, ...m.placement }; }}
              class="flex-1">
              {#each vacMap?.maps ?? [] as m (m.map_id)}
                <option value={m.map_id}>{m.map_name || m.map_id}{m.placement?.floor ? ` · ${m.placement.floor}` : ""}</option>
              {/each}
            </select>
          </label>
        {/if}
        <label class="mb-1 flex items-center gap-2">
          <span class="w-14 shrink-0 text-dida-text-muted">{t("fp.size")}</span>
          <input type="range" min="10" max="150" step="0.5" value={vacPlace.w} class="flex-1 accent-dida-accent"
            oninput={(e) => (vacPlace = { ...vacPlace, w: +e.currentTarget.value })} />
          <span class="w-10 text-right tabular-nums">{formatNumber(vacPlace.w, { maximumFractionDigits: 0 })}%</span>
        </label>
        <!-- Quarter turns only. A plan and a robot's map are both drawn along the
             walls, so the angle between them is a right angle or a mistake — and a
             slider that can sit at 3.5° only ever finds the mistake. -->
        <div class="mb-1 flex items-center gap-2">
          <span class="w-14 shrink-0 text-dida-text-muted">{t("fp.turn")}</span>
          <Button size="small" onclick={() => (vacPlace = { ...vacPlace, rotation: quarter(vacPlace.rotation, -1) })} title={t("fp.turnLeft")} label={t("fp.turnLeft")}>↺</Button>
          <span class="flex-1 text-center tabular-nums">{quarter(vacPlace.rotation, 0)}°</span>
          <Button size="small" onclick={() => (vacPlace = { ...vacPlace, rotation: quarter(vacPlace.rotation, 1) })} title={t("fp.turnRight")} label={t("fp.turnRight")}>↻</Button>
        </div>
        <label class="mb-1 flex items-center gap-2">
          <span class="w-14 shrink-0 text-dida-text-muted">{t("fp.fade")}</span>
          <input type="range" min="0.15" max="1" step="0.05" value={vacPlace.opacity ?? 0.55} class="flex-1 accent-dida-accent"
            oninput={(e) => (vacPlace = { ...vacPlace, opacity: +e.currentTarget.value })} />
        </label>
        <label class="mb-2 flex items-center gap-2">
          <input type="checkbox" checked={vacPlace.mirrored} class="accent-dida-accent"
            onchange={(e) => (vacPlace = { ...vacPlace, mirrored: e.currentTarget.checked })} />
          <span>{t("fp.mirror")}</span>
        </label>
        <p class="mb-2 text-dida-text-faint">{t("fp.impliesWidth", { m: formatNumber(planMetres, { maximumFractionDigits: 1 }) })}</p>
        {#if vacErr}<p class="mb-2 text-dida-danger">{vacErr}</p>{/if}
        <div class="flex gap-2">
          <div class="grid flex-1"><SaveButton size="small" dirty={vacDirty} saving={vacSaving} onclick={saveVacPlacement} /></div>
          <Button size="small" onclick={() => { vacEdit = false; if (vacMap?.placement) vacPlace = { ...vacPlace, ...vacMap.placement }; }}>{t("fp.cancel")}</Button>
        </div>
      </div>
    {/if}
    <!-- PLAN mode, layer 2 — borders: red rects (drag to move, handles resize); during a
         merge preview the thinned set renders and the erased piece pulses red -->
    {#if edit && mode === "plan"}
      {#if addingBorder}
        <!-- svelte-ignore a11y_no_static_element_interactions -- draw-an-area overlay, edit mode only: the gesture IS a pointer path across the image -->
        <div class="absolute inset-0 z-[15] cursor-crosshair"
          onpointerdown={onAddDown} onpointermove={onAddMove} onpointerup={onAddUp}></div>
      {/if}
      <!-- the svg ROOT box would swallow every click over the plan (a transparent HTML
           box still hit-tests), killing the room layer underneath — so the root passes
           events through and only the interactive children opt back in -->
      <svg viewBox="0 0 100 100" preserveAspectRatio="none"
        class="pointer-events-none absolute inset-0 z-[16] h-full w-full">
        <!-- all border rects OPAQUE inside one group: overlaps merge (no darkening at
             junctions), then the group opacity makes the whole union semi-transparent —
             so the borders read as ONE solid shape -->
        <g opacity="0.62" class={mergePreview || addingBorder ? "" : "pointer-events-auto"}>
          {#each (mergePreview?.borders ?? borderDraft) as b, i (i)}
            <rect x={b[0]} y={b[1]} width={b[2]} height={b[3]}
              fill={selBorder === i && !mergePreview ? "#2dd4bf" : "#dc2626"}
              class="cursor-move" role="button" tabindex="-1" aria-label={t("fp.borders")}
              onpointerdown={(e) => onBorderDown(e, i, "move")} onpointermove={onBorderMove} onpointerup={onBorderUp} />
          {/each}
        </g>
        {#if mergePreview}
          {#each mergePreview.removed as r, i (i)}
            <rect x={r[0]} y={r[1]} width={r[2]} height={r[3]} fill="#f87171" opacity="0.9" class="animate-pulse" />
          {/each}
        {/if}
        {#if drawBorder}
          <!-- solid, no stroke — matches the committed borders so the preview reads single -->
          <rect x={drawBorder[0]} y={drawBorder[1]} width={drawBorder[2]} height={drawBorder[3]}
            fill="#2dd4bf" opacity="0.62" />
        {/if}
        {#if snapDot}
          <circle cx={snapDot.x} cy={snapDot.y} r="1" fill="none" stroke="#2dd4bf" stroke-width="0.35" />
        {/if}
        {#if selBorder != null && borderDraft[selBorder] && !mergePreview}
          {@const sb = borderDraft[selBorder]}
          {#each handlesFor(sb) as h (h.k)}
            <rect x={h.x - 0.9} y={h.y - 0.9} width="1.8" height="1.8"
              fill="#2dd4bf" stroke="#0b0f14" stroke-width="0.15" class="pointer-events-auto cursor-pointer" role="button" tabindex="-1" aria-label="resize"
              onpointerdown={(e) => onBorderDown(e, selBorder as number, h.k)} onpointermove={onBorderMove} onpointerup={onBorderUp} />
          {/each}
        {/if}
      </svg>
    {/if}

    <!-- placement click surface (a real button for a11y) -->
    {#if edit && mode === "devices" && picking}
      <button type="button" onclick={onPlaceClick} aria-label={t("fp.clickToPlaceDevice", { name: items.find((i) => i.key === picking)?.name ?? "" })} class="absolute inset-0 z-10 cursor-crosshair"></button>
    {/if}

    <!-- device markers: pixel-sized on a %-positioned plan, so on narrow screens
         they'd relatively double and collide — shrink them as the plan shrinks -->
    {#each placedItems as { it, p } (it.key)}
      {#if p}
        <!-- svelte-ignore a11y_no_static_element_interactions -- device marker: drag to place in edit mode, hold to inspect otherwise; both are pointer gestures over a plan -->
        <div style="left:{p.x}%; top:{p.y}%; transform: translate(-50%,-50%) scale(calc({it.scale * markerScale} * var(--fp-mark, 1)))"
          class:hidden={!edit && mediaIdle(it)}
          class="@max-xl:[--fp-mark:0.95] @max-md:[--fp-mark:0.85] absolute z-30 {edit && mode !== 'devices' ? 'pointer-events-none opacity-60' : ''} {edit ? (dragKey === it.key ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-pointer'}"
          onpointerdown={(e) => onDown(e, it)} onpointermove={(e) => onMove(e, it)} onpointerup={(e) => onUp(e, it)}
          onpointercancel={clearPress} onpointerleave={clearPress}
          onkeydown={(e) => { if (!edit && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); shortTap(it); } }}
          role="button" tabindex="0" aria-label={it.name}>
          {#if (it.slot === "motion" || isSensorLabel(it)) && !isPlug(it)}
            <!-- The motion slot IS a presence indicator (hides when empty in view
                 mode) — force the label path even when the entity also carries a
                 non-readout cap (a BABA camera's `camera` descriptor would otherwise
                 route its aggregate dot to the always-visible FloorMarker). -->
            <FloorSensorLabel members={it.members} interactive={!edit} show={it.labelShow ?? "both"}
              onhistory={(entityId, cap) => openValueHistory(it, entityId, cap)} />
          {:else}
            <FloorMarker members={it.members} icon={it.icon} name={it.name} plug={isPlug(it)} tint={it.tint} selected={selected?.key === it.key || configItem?.key === it.key} />
          {/if}
          {#if edit && mode === "devices"}
            <button type="button" onpointerdown={(e) => e.stopPropagation()} onclick={(e) => { e.stopPropagation(); clearItem(it); }}
              aria-label={t("fp.removeFromPlan")} class="absolute -right-1.5 -top-1.5 grid size-4 place-items-center rounded-full border border-dida-danger/60 bg-dida-danger/90 text-2xs leading-none text-white">✕</button>
          {/if}
        </div>
      {/if}
    {/each}

    <!-- ROOM labels: one aggregated reading (temperature by default) per room; tap to
         expand every value; drag (edit) to reposition. -->
    {#each roomLabels as rl (rl.area.id)}
      <!-- svelte-ignore a11y_no_static_element_interactions -- room label, dragged to position in edit mode -->
      <div style="left:{rl.x}%; top:{rl.y}%; transform: translate(-50%,-50%) scale(calc({markerScale} * var(--fp-mark, 1)))"
        class="@max-xl:[--fp-mark:0.95] @max-md:[--fp-mark:0.85] absolute z-[28] {edit && mode !== 'devices' ? 'pointer-events-none opacity-60' : ''} {edit ? 'cursor-grab' : 'cursor-pointer'}"
        onpointerdown={(e) => onRoomDown(e, rl)} onpointermove={(e) => onRoomMove(e, rl)} onpointerup={(e) => onRoomUp(e, rl)}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); roomOpen = roomOpen?.area.id === rl.area.id ? null : rl; } }}
        role="button" tabindex="0" aria-label={devices.roomLabel(rl.area)}>
        <RoomSensorLabel caps={rl.caps} />
      </div>
    {/each}

    <!-- floor switch: ONE control at ONE spot on every floor (the stairwell doesn't
         move between levels) — shows the CURRENT floor, tap cycles the value, drag
         (while editing) repositions it. Per-client navigation. -->
    {#if nextFloor}
      <!-- svelte-ignore a11y_no_static_element_interactions -- floor-switch marker, dragged to position in edit mode -->
      <div style="left:{switchPos.x}%; top:{switchPos.y}%"
        class="absolute z-[26] -translate-x-1/2 -translate-y-1/2 {edit ? 'cursor-grab' : 'cursor-pointer'}"
        onpointerdown={onSwitchDown} onpointermove={onSwitchMove} onpointerup={onSwitchUp}
        onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchFloor(); } }}
        role="button" tabindex="0" aria-label={t("fp.switchFloor")}>
        <div class="flex items-center gap-1.5 rounded-full border border-dida-border bg-dida-panel/85 px-2.5 py-1.5 shadow backdrop-blur-sm hover:border-dida-accent">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-4 text-dida-accent"><path d="M3 20h4v-4h4v-4h4V8h4V4" /></svg>
          <span class="whitespace-nowrap text-xs font-semibold">{curFloor?.name}</span>
        </div>
      </div>
    {/if}

    <!-- hover room label -->
    {#if hover}
      <span style="left:{hover.x}%; top:{hover.y}%" class="pointer-events-none absolute z-[25] -translate-x-1/2 -translate-y-[150%] whitespace-nowrap rounded-full bg-black/60 px-2 py-0.5 text-2xs font-semibold uppercase tracking-wide text-white/90 backdrop-blur-sm">{hover.name}</span>
    {/if}

    <!-- backdrops + popovers, anchored to the marker at every size (aPos clamps them
         inside the plan) — identical on phone and desktop. -->
    <!-- One consistent close affordance for every popover: a floating ✕ pinned to
         the card's top-right (the invisible backdrop still closes on tap too). -->
    {#snippet closeX(onClose: () => void)}
      <button type="button" onclick={onClose} aria-label={t("fp.close")}
        class="absolute right-1 top-1 z-[2] flex size-6 items-center justify-center rounded-full bg-dida-panel-2/90 text-m leading-none text-dida-text-muted shadow ring-1 ring-dida-border/60 hover:text-dida-text">✕</button>
    {/snippet}
    {#if roomSel}
      <button type="button" onclick={() => (roomSel = null)} aria-label={t("fp.close")} class="absolute inset-0 z-[22]"></button>
      <div style={aPos(roomSel.cx, roomSel.cy)}
        class="absolute z-40 w-56 max-w-[calc(100%-1rem)] rounded-lg border border-dida-border bg-dida-panel p-3 shadow-xl">
        {@render closeX(() => (roomSel = null))}
        {@render roomOptions(roomSel)}
      </div>
    {/if}
    {#if selected}
      {@const sp = itemPos(selected)}
      <button type="button" onclick={() => (selected = null)} aria-label={t("fp.close")} class="absolute inset-0 z-20"></button>
      {#if sp}
        <div style={aPos(sp.x, sp.y)} class="absolute z-40 w-72 max-w-[calc(100%-1rem)] rounded-lg">
          {@render closeX(() => (selected = null))}
          <div class="max-h-[75vh] overflow-y-auto rounded-lg">
            <FloorControl members={selected.members} name={selected.name} icon={selected.icon} showToggle={opensControls(selected) && selected.toggle != null}
              onHistory={(entityId, cap) => selected && openValueHistory(selected, entityId, cap)}
              ondetail={(entityId) => { detailId = entityId; selected = null; }} />
            {#if selected.members.some((m) => "vacuum" in m.caps)}
              <div class="mt-2 flex gap-2 rounded-lg border border-dida-border bg-dida-panel p-2">
                <div class="flex-1 *:w-full"><Button size="small" onclick={async () => {
                    sendMode = true; sendSpot = null; selected = null;
                    vacShow = true; if (!vacMap) await loadVacMap();
                  }} disabled={!planLinked} title={planLinked ? "" : t("fp.calibrateFirst")}>{t("fp.cleanSpot")}</Button></div>


              </div>
            {/if}
          </div>
        </div>
      {/if}
    {/if}
    {#if configItem}
      {@const cp = itemPos(configItem)}
      <button type="button" onclick={() => (configItem = null)} aria-label={t("fp.close")} class="absolute inset-0 z-20"></button>
      {#if cp}
        <div style={aPos(cp.x, cp.y)} class="absolute z-40 w-60 max-w-[calc(100%-1rem)] rounded-lg border border-dida-border bg-dida-panel p-3 shadow-xl">
          {@render closeX(() => (configItem = null))}
          {@render devConfig(configItem)}
        </div>
      {/if}
    {/if}
    {#if roomOpen && roomOpenLive}
      <button type="button" onclick={() => (roomOpen = null)} aria-label={t("fp.close")} class="absolute inset-0 z-[22]"></button>
      <div style={aPos(roomOpenLive.x, roomOpenLive.y)} class="absolute z-40 w-56 max-w-[calc(100%-1rem)] rounded-lg border border-dida-border bg-dida-panel p-3 shadow-xl">
        {@render closeX(() => (roomOpen = null))}
        {@render roomPanel(roomOpenLive)}
      </div>
    {/if}
    {#if hist}
      <!-- Close only a press that STARTED on the backdrop: history opens mid-hold
           (pointer still down on the value row), so the release fires a stray click
           on this topmost layer — an unarmed click must not insta-close the popover. -->
      <button type="button" onpointerdown={() => (histArm = true)}
        onclick={() => { if (histArm) hist = null; histArm = false; }}
        aria-label={t("fp.close")} class="absolute inset-0 z-40"></button>
      <div style={aPos(hist.x, hist.y)} class="absolute z-[41] w-72 max-w-[calc(100%-1rem)]">
        {@render closeX(() => { hist = null; histArm = false; })}
        <!-- A simple device (one on_off light) never opens the control popover — a
             tap just toggles it — so this history card is its ONLY surface, and the
             ⓘ here is how the detail panel is reached for exactly those devices.
             Same affordance and same place as the card and the control popover. -->
        <FloorHistory readings={hist.readings} cap={hist.cap} name={hist.name}
          ondetail={(entityId) => { detailId = entityId; hist = null; histArm = false; }} />
      </div>
    {/if}
    {#if camSnap}
      <button type="button" onclick={() => (camSnap = null)} aria-label={t("fp.close")} class="absolute inset-0 z-40"></button>
      <div style={aPos(camSnap.x, camSnap.y)} class="absolute z-[41] w-80 max-w-[calc(100%-1rem)] overflow-hidden rounded-lg border border-dida-border bg-dida-panel shadow-xl">
        {@render closeX(() => (camSnap = null))}
        <img src={`/api/camera/${encodeURIComponent(camSnap.eid)}/snapshot?w=480&t=${camTick}`}
          alt={camSnap.name} class="block aspect-video w-full bg-black object-contain" />
        <p class="px-3 py-1.5 text-s text-dida-text-muted">{camSnap.name}</p>
      </div>
    {/if}
  </div>

  {#if edit}
    <!-- edit toolbox: a card next to the plan on desktop; below lg it docks to the
         bottom of the screen (thumb-reachable, no scrolling between plan and tools) -->
    <div class="w-full shrink-0 rounded-lg border border-dida-border bg-dida-panel p-3 max-lg:fixed max-lg:inset-x-0 max-lg:bottom-0 max-lg:z-30 max-lg:max-h-[42vh] max-lg:overflow-y-auto max-lg:rounded-b-none max-lg:rounded-t-2xl max-lg:border-x-0 max-lg:border-b-0 max-lg:pb-[max(0.75rem,env(safe-area-inset-bottom))] lg:w-64">
      <div class="mb-3">
        <Picks
          picks={[{ key: "plan", label: t("fp.tabPlan") }, { key: "devices", label: t("fp.tabDevices") }]}
          chosen={[mode]}
          onpick={(k) => {
            if (k === "plan") { mode = "plan"; picking = null; }
            else { mode = "devices"; addingBorder = false; drawBorder = null; selBorder = null; roomSel = null; mergeFrom = null; mergePreview = null; }
          }}
        />
      </div>

      {#if mode === "devices"}
        <div class="mb-3">
          <p class="mb-1 flex items-baseline justify-between text-xs font-medium uppercase tracking-wide text-dida-text-muted">
            <span>{t("fp.markerScale")}</span><span class="font-semibold normal-case text-dida-text">{formatNumber(markerScale, { maximumFractionDigits: 2 })}×</span></p>
          <input type="range" min="0.5" max="2" step="0.05" value={markerScale}
            oninput={(e) => setMarkerScaleLocal(Number(e.currentTarget.value))}
            onchange={(e) => saveMarkerScale(Number(e.currentTarget.value))} class="w-full" />
          <div class="mt-1 *:w-full"><Button size="small" onclick={uniformSize} disabled={!anyResized}>{t("fp.uniformSize")}</Button></div>
        </div>
        {#if roomLabelAreas.length}
          <p class="mb-1 text-xs font-medium uppercase tracking-wide text-dida-text-muted">{t("fp.roomLabels")}</p>
          <div class="mb-3 flex flex-col gap-0.5">
            {#each roomLabelAreas as rl (rl.area.id)}
              <label class="flex items-center gap-2 rounded px-1 py-0.5 text-s text-dida-text-muted hover:bg-dida-panel-2">
                <input type="checkbox" checked={!rl.off} onchange={() => toggleRoomLabel(rl.area, !rl.off)} class="shrink-0 accent-dida-accent" />
                <span class="truncate">{devices.roomLabel(rl.area)}</span>
              </label>
            {/each}
          </div>
        {/if}
        <p class="mb-2 {SUBSECTION_TITLE_CLASS}">{t("fp.devicesToPlace", { n: unplacedCount })}</p>
        {#if picking}<p class="mb-2 rounded bg-dida-accent/10 px-2 py-1 text-s text-dida-accent">{t("fp.clickToPlaceDevice", { name: items.find((i) => i.key === picking)?.name ?? "" })}</p>{/if}
        <input bind:value={q} placeholder={t("fp.searchDevices")} class="mb-2 w-full" />
        <div class="flex max-h-[24rem] flex-col gap-2 overflow-auto">
          {#each groups as g (g.areaId ?? "none")}
            <div>
              <p class="mb-1 px-0.5 text-xs font-semibold uppercase tracking-wide text-dida-text-faint">{g.name}</p>
              <div class="flex flex-col gap-1">
                {#each g.items as it (it.key)}
                  <button onclick={() => (picking = it.key)} class="flex items-center gap-2 rounded border px-2 py-1.5 text-left text-s {picking === it.key ? 'border-dida-accent bg-dida-accent/10 text-dida-accent' : 'border-dida-border bg-dida-panel-2 text-dida-text hover:border-dida-accent'}">
                    <DeviceIcon type={it.icon} class="size-3.5 shrink-0 text-dida-text-muted" />
                    <span class="truncate">{it.name}</span>
                  </button>
                {/each}
              </div>
            </div>
          {:else}
            <p class="text-s text-dida-text-faint">{t("fp.allPlaced")}</p>
          {/each}
        </div>
      {:else}
        <!-- plan: borders are the source of truth; rooms derive live from the draft -->
        <p class="mb-2 {SUBSECTION_TITLE_CLASS}">{t("fp.borders")}</p>
        {#if floorImg}
          <div class="mb-2 *:w-full"><Button size="small" tone="accent" onclick={detectBordersFn} disabled={borderBusy}>            {borderBusy ? t("fp.detecting") : t("fp.detectBorders")}</Button></div>
        {:else}
          <p class="mb-2 text-xs text-dida-text-faint">{t("fp.noScaffold")}</p>
        {/if}
        <div class="mb-2 flex gap-1">
          <div class="flex-1 *:w-full"><Button size="small" selected={addingBorder} onclick={() => { addingBorder = !addingBorder; selBorder = null; roomSel = null; mergeFrom = null; }}>{t("fp.addBorder")}</Button></div>
          <Button size="small" tone="danger" onclick={() => selBorder != null && deleteBorder(selBorder)} disabled={selBorder == null}>{t("fp.deleteBorder")}</Button>
        </div>
        <div class="mb-2 flex gap-1">
          <div class="flex-1 *:w-full"><Button size="small" onclick={undo} disabled={!undoStack.length}>↶ {t("fp.undo")}</Button></div>
          <div class="grid flex-1"><SaveButton size="small" dirty={bordersDirty} saving={borderBusy} onclick={saveBordersFn} /></div>
        </div>
        <p class="mb-2 text-xs text-dida-text-faint">{t("fp.bordersHint")} · {borderDraft.length}{deriving ? ` · ${t("fp.detecting")}` : ""}</p>

        {#if mergeFrom}
          <div class="mb-2 flex items-center gap-2 rounded bg-dida-accent/10 px-2 py-1.5">
            <p class="flex-1 text-s text-dida-accent">{t("fp.mergePick")}</p>
            <Button size="small" onclick={() => (mergeFrom = null)}>{t("common.cancel")}</Button>
          </div>
        {/if}
        {#if mergePreview}
          <div class="mb-2 rounded border border-dida-border bg-dida-panel-2 p-2">
            <p class="mb-2 text-xs text-dida-text-muted">{t("fp.mergePreview")}</p>
            <div class="flex gap-1">
              <div class="flex-1 *:w-full"><Button size="small" tone="accent" onclick={confirmMerge}>{t("fp.mergeConfirm")}</Button></div>
              <div class="flex-1 *:w-full"><Button size="small" onclick={() => (mergePreview = null)}>{t("common.cancel")}</Button></div>
            </div>
          </div>
        {/if}
        {#if mergeMsg}
          <p class="mb-2 rounded bg-dida-danger/10 px-2 py-1 text-s text-dida-danger">{mergeMsg}</p>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<!-- Users track: who's where, right under the plan. Same presence strip as the
     Devices page (one chip per person: name + current zone / "Vani", stale-faded
     once reports stop). Aligned to the plan's width; hidden while arranging. -->
{#if !edit}
  <PresencePanel class="mx-auto mt-4 w-full max-w-2xl" />
{/if}

<svelte:window onkeydown={onPlanKey} />

{#if edit}
  <p class="mt-3 text-s text-dida-text-faint max-lg:hidden">{mode === "plan" ? t("fp.help.plan") : t("fp.help.editDevices")}</p>
{/if}

<DeviceDetail entityId={detailId} onclose={() => (detailId = null)} />
