// What the media surface reads off a player. Almost all of it is parsing values the
// adapters put on the bus, and almost all of it fails the same way: the tile shows
// something plausible. A queue that will not parse becomes an empty playlist rather
// than an error; a position with no baseline extrapolates from zero and the scrubber
// walks away from the music.
import { describe, expect, it } from "vitest";
import {
  fmtTime, gradient, isActive, isAvrZone, isMediaPlayer, isPlaying, mediaDuration,
  mediaMuted, mediaQueue, mediaVolume, positionBase, sourceOptions, sourceValue,
  transport,
} from "$lib/media";

const cap = (v: unknown, updatedAt = 1) => ({ value: v, unit: null, updatedAt });
const dev = (caps: Record<string, unknown>, updatedAt = 1) =>
  ({ entityId: "x:1", name: "X", hiddenCaps: [],
     caps: Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, cap(v, updatedAt)])) }) as never;

describe("what counts as a player", () => {
  it("a transport makes it one", () => {
    expect(isMediaPlayer(dev({ media_transport: "playing" }))).toBe(true);
    expect(isMediaPlayer(dev({ on_off: true }))).toBe(false);
  });

  it("an AVR zone is controllable but NOT a player", () => {
    // The distinction the media page is built on: a zone gets volume and source,
    // never a scrubber or a play button that would do nothing.
    expect(isAvrZone(dev({ on_off: true, volume: 30 }))).toBe(true);
    expect(isAvrZone(dev({ media_transport: "playing", volume: 30 }))).toBe(false);
  });

  it("a device with neither is not a zone either", () => {
    expect(isAvrZone(dev({ temperature: 21 }))).toBe(false);
  });
});

describe("transport state", () => {
  it("is read as published", () => {
    expect(transport(dev({ media_transport: "paused" }))).toBe("paused");
  });

  it("a missing or non-string transport is idle, not playing", () => {
    // Defaulting the other way would leave a scrubber running on a silent device.
    expect(transport(dev({}))).toBe("idle");
    expect(transport(dev({ media_transport: 1 }))).toBe("idle");
  });

  it("buffering counts as ACTIVE but not as playing", () => {
    // The card stays up while a stream fills; the play button must not claim it is
    // already playing.
    const buffering = dev({ media_transport: "buffering" });
    expect(isActive(buffering)).toBe(true);
    expect(isPlaying(buffering)).toBe(false);
  });

  it("stopped is neither", () => {
    expect(isActive(dev({ media_transport: "stopped" }))).toBe(false);
  });
});

describe("the play queue", () => {
  it("is parsed from the adapter's JSON", () => {
    const q = mediaQueue(dev({ media_queue: JSON.stringify({ items: [{ title: "A" }], current: 0 }) }));
    expect(q.items).toHaveLength(1);
    expect(q.current).toBe(0);
  });

  it("unparseable JSON is an empty queue, not a crash", () => {
    // This runs inside a render; throwing here takes the whole media page down.
    expect(mediaQueue(dev({ media_queue: "{oops" }))).toEqual({ items: [], current: -1 });
  });

  it("a queue whose items are not a list is empty rather than half-read", () => {
    expect(mediaQueue(dev({ media_queue: JSON.stringify({ items: "nope", current: 2 }) })).items).toEqual([]);
  });

  it("no current index reads as -1, not as track zero", () => {
    // Zero is a track. -1 is "nothing highlighted".
    expect(mediaQueue(dev({ media_queue: JSON.stringify({ items: [] }) })).current).toBe(-1);
  });

  it("no queue at all is empty", () => {
    expect(mediaQueue(dev({}))).toEqual({ items: [], current: -1 });
  });
});

describe("the position baseline", () => {
  it("carries the value AND the moment it was published", () => {
    // The scrubber extrapolates from this pair; without the timestamp it would
    // restart from the last poll every second.
    const base = positionBase(dev({ media_position: 42 }, 1_700_000_000_000));
    expect(base).toEqual({ pos: 42, at: 1_700_000_000_000 });
  });

  it("is null when the player publishes no position", () => {
    // Null, not zero: zero would drag the scrubber back to the start of the track.
    expect(positionBase(dev({}))).toBeNull();
    expect(positionBase(dev({ media_position: "42" }))).toBeNull();
  });
});

describe("numbers off a player", () => {
  it("volume is null when absent, so the slider hides instead of showing 0", () => {
    expect(mediaVolume(dev({ volume: 30 }))).toBe(30);
    expect(mediaVolume(dev({}))).toBeNull();
  });

  it("mute is only true when it is exactly true", () => {
    expect(mediaMuted(dev({ mute: true }))).toBe(true);
    expect(mediaMuted(dev({ mute: "true" }))).toBe(false);
    expect(mediaMuted(dev({}))).toBe(false);
  });

  it("duration falls back to zero, which is what an unknown-length stream is", () => {
    expect(mediaDuration(dev({ media_duration: 245 }))).toBe(245);
    expect(mediaDuration(dev({}))).toBe(0);
  });
});

describe("clock formatting", () => {
  it("mm:ss under an hour, h:mm:ss past it", () => {
    expect(fmtTime(65)).toBe("1:05");
    expect(fmtTime(3661)).toBe("1:01:01");
  });

  it("pads the minutes only once there are hours", () => {
    expect(fmtTime(605)).toBe("10:05");
    expect(fmtTime(3605)).toBe("1:00:05");
  });

  it("nonsense is zero rather than NaN:NaN on the tile", () => {
    expect(fmtTime(Number.NaN)).toBe("0:00");
    expect(fmtTime(-5)).toBe("0:00");
    expect(fmtTime(Number.POSITIVE_INFINITY)).toBe("0:00");
  });
});

describe("the art fallback gradient", () => {
  it("is deterministic — the same station keeps its colour between renders", () => {
    expect(gradient("Radio 1")).toBe(gradient("Radio 1"));
  });

  it("different seeds get different colours", () => {
    expect(gradient("Radio 1")).not.toBe(gradient("Radio 2"));
  });

  it("an empty seed still produces a valid gradient", () => {
    expect(gradient("")).toMatch(/^linear-gradient\(135deg, hsl\(\d+ /);
  });
});

describe("AVR source selection", () => {
  it("the options come from the adapter's JSON list", () => {
    expect(sourceOptions(dev({ source_options: JSON.stringify(["MUSIC", "TV"]) }))).toEqual(["MUSIC", "TV"]);
  });

  it("unparseable options are empty, not a dropdown of garbage", () => {
    expect(sourceOptions(dev({ source_options: "MUSIC,TV" }))).toEqual([]);
    expect(sourceOptions(dev({}))).toEqual([]);
  });

  it("a JSON value that is not a list is empty too", () => {
    expect(sourceOptions(dev({ source_options: JSON.stringify({ a: 1 }) }))).toEqual([]);
  });

  it("the current source is null when unset, so nothing is preselected", () => {
    expect(sourceValue(dev({ source: "MUSIC" }))).toBe("MUSIC");
    expect(sourceValue(dev({}))).toBeNull();
  });
});
